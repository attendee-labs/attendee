from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import (
    ApiKey,
    Bot,
    BotLogin,
    BotLoginGroup,
    BotLoginPlatform,
    Calendar,
    CalendarEvent,
    CalendarPlatform,
    Project,
    ProjectAccess,
    WebhookSubscription,
    WebhookTriggerTypes,
    ZoomOAuthApp,
)
from bots.projects_views import (
    get_api_key_for_user,
    get_calendar_event_for_user,
    get_calendar_for_user,
    get_bot_login_for_user,
    get_project_for_user,
    get_webhook_subscription_for_user,
)


class ObjectAccessIntegrationTest(TransactionTestCase):
    """Integration tests for object access control in projects_views.py"""

    def setUp(self):
        """Set up test environment with multiple organizations, users, and projects"""

        # Create two organizations
        self.organization_a = Organization.objects.create(name="Organization A", centicredits=10000)
        self.organization_b = Organization.objects.create(name="Organization B", centicredits=10000)

        # Create users in Organization A
        self.admin_user_a = User.objects.create_user(username="admin_a", email="admin_a@example.com", password="testpassword123", role=UserRole.ADMIN, organization=self.organization_a)

        self.regular_user_a = User.objects.create_user(username="regular_a", email="regular_a@example.com", password="testpassword123", role=UserRole.REGULAR_USER, organization=self.organization_a)

        self.regular_user_a2 = User.objects.create_user(username="regular_a2", email="regular_a2@example.com", password="testpassword123", role=UserRole.REGULAR_USER, organization=self.organization_a)

        # Create users in Organization B
        self.admin_user_b = User.objects.create_user(username="admin_b", email="admin_b@example.com", password="testpassword123", role=UserRole.ADMIN, organization=self.organization_b)

        self.regular_user_b = User.objects.create_user(username="regular_b", email="regular_b@example.com", password="testpassword123", role=UserRole.REGULAR_USER, organization=self.organization_b)

        # Create projects in Organization A
        self.project_a1 = Project.objects.create(name="Project A1", organization=self.organization_a)

        self.project_a2 = Project.objects.create(name="Project A2", organization=self.organization_a)

        # Create projects in Organization B
        self.project_b1 = Project.objects.create(name="Project B1", organization=self.organization_b)

        # Give regular_user_a access to project_a1 only
        ProjectAccess.objects.create(project=self.project_a1, user=self.regular_user_a)

        # Give regular_user_b access to project_b1
        ProjectAccess.objects.create(project=self.project_b1, user=self.regular_user_b)

        # regular_user_a2 has no project access

        # Create test objects for access testing
        self._create_test_objects()

        # Create test client
        self.client = Client()

    def _create_test_objects(self):
        """Create test objects (calendars, bots, api keys, etc.) for access testing"""

        # Create calendars
        self.calendar_a1 = Calendar.objects.create(project=self.project_a1, platform=CalendarPlatform.GOOGLE, client_id="test_client_id_a1")

        self.calendar_a2 = Calendar.objects.create(project=self.project_a2, platform=CalendarPlatform.GOOGLE, client_id="test_client_id_a2")

        self.calendar_b1 = Calendar.objects.create(project=self.project_b1, platform=CalendarPlatform.GOOGLE, client_id="test_client_id_b1")

        # Create calendar events
        from datetime import datetime, timezone

        self.calendar_event_a1 = CalendarEvent.objects.create(calendar=self.calendar_a1, platform_uuid="event_a1", start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc), raw={})

        self.calendar_event_a2 = CalendarEvent.objects.create(calendar=self.calendar_a2, platform_uuid="event_a2", start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc), raw={})

        self.calendar_event_b1 = CalendarEvent.objects.create(calendar=self.calendar_b1, platform_uuid="event_b1", start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc), raw={})

        # Create bots
        self.bot_a1 = Bot.objects.create(project=self.project_a1, name="Bot A1", meeting_url="https://zoom.us/j/1234567890")

        self.bot_a2 = Bot.objects.create(project=self.project_a2, name="Bot A2", meeting_url="https://zoom.us/j/0987654321")

        self.bot_b1 = Bot.objects.create(project=self.project_b1, name="Bot B1", meeting_url="https://zoom.us/j/1111111111")

        # Create API keys
        self.api_key_a1, _ = ApiKey.create(project=self.project_a1, name="API Key A1")
        self.api_key_a2, _ = ApiKey.create(project=self.project_a2, name="API Key A2")
        self.api_key_b1, _ = ApiKey.create(project=self.project_b1, name="API Key B1")

        # Create webhook subscriptions
        self.webhook_a1 = WebhookSubscription.objects.create(project=self.project_a1, url="https://example.com/webhook/a1", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])

        self.webhook_a2 = WebhookSubscription.objects.create(project=self.project_a2, url="https://example.com/webhook/a2", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])

        self.webhook_b1 = WebhookSubscription.objects.create(project=self.project_b1, url="https://example.com/webhook/b1", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])

        # Create Zoom OAuth apps
        self.zoom_oauth_app_a1 = ZoomOAuthApp.objects.create(project=self.project_a1, client_id="test_client_id_a1")
        self.zoom_oauth_app_a1.set_credentials({"client_secret": "test_secret_a1", "webhook_secret": "test_webhook_secret_a1"})

        self.zoom_oauth_app_a2 = ZoomOAuthApp.objects.create(project=self.project_a2, client_id="test_client_id_a2")
        self.zoom_oauth_app_a2.set_credentials({"client_secret": "test_secret_a2", "webhook_secret": "test_webhook_secret_a2"})

        self.zoom_oauth_app_b1 = ZoomOAuthApp.objects.create(project=self.project_b1, client_id="test_client_id_b1")
        self.zoom_oauth_app_b1.set_credentials({"client_secret": "test_secret_b1", "webhook_secret": "test_webhook_secret_b1"})

        # Create Google Meet bot login groups and logins
        self.google_meet_bot_login_group_a1 = BotLoginGroup.objects.create(project=self.project_a1, platform=BotLoginPlatform.GOOGLE_MEET, name="Google Meet Group 1 A1")
        self.google_meet_bot_login_a1 = BotLogin.objects.create(
            group=self.google_meet_bot_login_group_a1,
            workspace_domain="workspace-a1.com",
            email="bot-a1@workspace-a1.com",
        )
        self.google_meet_bot_login_a1.set_credentials({"private_key": "test_private_key_a1", "cert": "test_cert_a1"})

        self.google_meet_bot_login_group_a2 = BotLoginGroup.objects.create(project=self.project_a2, platform=BotLoginPlatform.GOOGLE_MEET, name="Google Meet Group 1 A2")
        self.google_meet_bot_login_a2 = BotLogin.objects.create(
            group=self.google_meet_bot_login_group_a2,
            workspace_domain="workspace-a2.com",
            email="bot-a2@workspace-a2.com",
        )
        self.google_meet_bot_login_a2.set_credentials({"private_key": "test_private_key_a2", "cert": "test_cert_a2"})

        self.google_meet_bot_login_group_b1 = BotLoginGroup.objects.create(project=self.project_b1, platform=BotLoginPlatform.GOOGLE_MEET, name="Google Meet Group 1 B1")
        self.google_meet_bot_login_b1 = BotLogin.objects.create(
            group=self.google_meet_bot_login_group_b1,
            workspace_domain="workspace-b1.com",
            email="bot-b1@workspace-b1.com",
        )
        self.google_meet_bot_login_b1.set_credentials({"private_key": "test_private_key_b1", "cert": "test_cert_b1"})

        # Create Teams bot login groups and logins
        self.teams_bot_login_group_a1 = BotLoginGroup.objects.create(project=self.project_a1, platform=BotLoginPlatform.TEAMS, name="Teams Group 1 A1")
        self.teams_bot_login_a1 = BotLogin.objects.create(
            group=self.teams_bot_login_group_a1,
            email="teams-a1@example.com",
        )
        self.teams_bot_login_a1.set_credentials({"password": "test_password_a1"})

        self.teams_bot_login_group_a2 = BotLoginGroup.objects.create(project=self.project_a2, platform=BotLoginPlatform.TEAMS, name="Teams Group 1 A2")
        self.teams_bot_login_a2 = BotLogin.objects.create(
            group=self.teams_bot_login_group_a2,
            email="teams-a2@example.com",
        )
        self.teams_bot_login_a2.set_credentials({"password": "test_password_a2"})

        self.teams_bot_login_group_b1 = BotLoginGroup.objects.create(project=self.project_b1, platform=BotLoginPlatform.TEAMS, name="Teams Group 1 B1")
        self.teams_bot_login_b1 = BotLogin.objects.create(
            group=self.teams_bot_login_group_b1,
            email="teams-b1@example.com",
        )
        self.teams_bot_login_b1.set_credentials({"password": "test_password_b1"})

    # Tests for get_project_for_user()
    def test_get_project_for_user_admin_access_same_org(self):
        """Test that admin users can access any project in their organization"""
        # Admin should be able to access any project in their org
        project = get_project_for_user(self.admin_user_a, self.project_a1.object_id)
        self.assertEqual(project, self.project_a1)

        project = get_project_for_user(self.admin_user_a, self.project_a2.object_id)
        self.assertEqual(project, self.project_a2)

    def test_get_project_for_user_admin_denied_different_org(self):
        """Test that admin users cannot access projects in different organizations"""
        with self.assertRaises(Http404):
            get_project_for_user(self.admin_user_a, self.project_b1.object_id)

    def test_get_project_for_user_regular_access_with_permission(self):
        """Test that regular users can access projects they have explicit access to"""
        project = get_project_for_user(self.regular_user_a, self.project_a1.object_id)
        self.assertEqual(project, self.project_a1)

    def test_get_project_for_user_regular_denied_no_permission(self):
        """Test that regular users cannot access projects they don't have explicit access to"""
        with self.assertRaises(PermissionDenied):
            get_project_for_user(self.regular_user_a, self.project_a2.object_id)

    def test_get_project_for_user_regular_denied_different_org(self):
        """Test that regular users cannot access projects in different organizations"""
        with self.assertRaises(Http404):
            get_project_for_user(self.regular_user_a, self.project_b1.object_id)

    def test_get_project_for_user_regular_no_access_to_any_project(self):
        """Test that regular users with no project access cannot access any project"""
        with self.assertRaises(PermissionDenied):
            get_project_for_user(self.regular_user_a2, self.project_a1.object_id)

        with self.assertRaises(PermissionDenied):
            get_project_for_user(self.regular_user_a2, self.project_a2.object_id)

    # Tests for get_calendar_for_user()
    def test_get_calendar_for_user_admin_access_same_org(self):
        """Test that admin users can access any calendar in their organization"""
        calendar = get_calendar_for_user(self.admin_user_a, self.calendar_a1.object_id)
        self.assertEqual(calendar, self.calendar_a1)

        calendar = get_calendar_for_user(self.admin_user_a, self.calendar_a2.object_id)
        self.assertEqual(calendar, self.calendar_a2)

    def test_get_calendar_for_user_admin_denied_different_org(self):
        """Test that admin users cannot access calendars in different organizations"""
        with self.assertRaises(Http404):
            get_calendar_for_user(self.admin_user_a, self.calendar_b1.object_id)

    def test_get_calendar_for_user_regular_access_with_permission(self):
        """Test that regular users can access calendars in projects they have access to"""
        calendar = get_calendar_for_user(self.regular_user_a, self.calendar_a1.object_id)
        self.assertEqual(calendar, self.calendar_a1)

    def test_get_calendar_for_user_regular_denied_no_permission(self):
        """Test that regular users cannot access calendars in projects they don't have access to"""
        with self.assertRaises(PermissionDenied):
            get_calendar_for_user(self.regular_user_a, self.calendar_a2.object_id)

    def test_get_calendar_for_user_regular_denied_different_org(self):
        """Test that regular users cannot access calendars in different organizations"""
        with self.assertRaises(Http404):
            get_calendar_for_user(self.regular_user_a, self.calendar_b1.object_id)

    # Tests for get_calendar_event_for_user()
    def test_get_calendar_event_for_user_admin_access_same_org(self):
        """Test that admin users can access any calendar event in their organization"""
        event = get_calendar_event_for_user(self.admin_user_a, self.calendar_event_a1.object_id)
        self.assertEqual(event, self.calendar_event_a1)

        event = get_calendar_event_for_user(self.admin_user_a, self.calendar_event_a2.object_id)
        self.assertEqual(event, self.calendar_event_a2)

    def test_get_calendar_event_for_user_admin_denied_different_org(self):
        """Test that admin users cannot access calendar events in different organizations"""
        with self.assertRaises(Http404):
            get_calendar_event_for_user(self.admin_user_a, self.calendar_event_b1.object_id)

    def test_get_calendar_event_for_user_regular_access_with_permission(self):
        """Test that regular users can access calendar events in projects they have access to"""
        event = get_calendar_event_for_user(self.regular_user_a, self.calendar_event_a1.object_id)
        self.assertEqual(event, self.calendar_event_a1)

    def test_get_calendar_event_for_user_regular_denied_no_permission(self):
        """Test that regular users cannot access calendar events in projects they don't have access to"""
        with self.assertRaises(PermissionDenied):
            get_calendar_event_for_user(self.regular_user_a, self.calendar_event_a2.object_id)

    def test_get_calendar_event_for_user_regular_denied_different_org(self):
        """Test that regular users cannot access calendar events in different organizations"""
        with self.assertRaises(Http404):
            get_calendar_event_for_user(self.regular_user_a, self.calendar_event_b1.object_id)

    # Tests for get_api_key_for_user()
    def test_get_api_key_for_user_admin_access_same_org(self):
        """Test that admin users can access any API key in their organization"""
        api_key = get_api_key_for_user(self.admin_user_a, self.api_key_a1.object_id)
        self.assertEqual(api_key, self.api_key_a1)

        api_key = get_api_key_for_user(self.admin_user_a, self.api_key_a2.object_id)
        self.assertEqual(api_key, self.api_key_a2)

    def test_get_api_key_for_user_admin_denied_different_org(self):
        """Test that admin users cannot access API keys in different organizations"""
        with self.assertRaises(Http404):
            get_api_key_for_user(self.admin_user_a, self.api_key_b1.object_id)

    def test_get_api_key_for_user_regular_access_with_permission(self):
        """Test that regular users can access API keys in projects they have access to"""
        api_key = get_api_key_for_user(self.regular_user_a, self.api_key_a1.object_id)
        self.assertEqual(api_key, self.api_key_a1)

    def test_get_api_key_for_user_regular_denied_no_permission(self):
        """Test that regular users cannot access API keys in projects they don't have access to"""
        with self.assertRaises(PermissionDenied):
            get_api_key_for_user(self.regular_user_a, self.api_key_a2.object_id)

    def test_get_api_key_for_user_regular_denied_different_org(self):
        """Test that regular users cannot access API keys in different organizations"""
        with self.assertRaises(Http404):
            get_api_key_for_user(self.regular_user_a, self.api_key_b1.object_id)

    # Tests for get_webhook_subscription_for_user()
    def test_get_webhook_subscription_for_user_admin_access_same_org(self):
        """Test that admin users can access any webhook subscription in their organization"""
        webhook = get_webhook_subscription_for_user(self.admin_user_a, self.webhook_a1.object_id)
        self.assertEqual(webhook, self.webhook_a1)

        webhook = get_webhook_subscription_for_user(self.admin_user_a, self.webhook_a2.object_id)
        self.assertEqual(webhook, self.webhook_a2)

    def test_get_webhook_subscription_for_user_admin_denied_different_org(self):
        """Test that admin users cannot access webhook subscriptions in different organizations"""
        with self.assertRaises(Http404):
            get_webhook_subscription_for_user(self.admin_user_a, self.webhook_b1.object_id)

    def test_get_webhook_subscription_for_user_regular_access_with_permission(self):
        """Test that regular users can access webhook subscriptions in projects they have access to"""
        webhook = get_webhook_subscription_for_user(self.regular_user_a, self.webhook_a1.object_id)
        self.assertEqual(webhook, self.webhook_a1)

    def test_get_webhook_subscription_for_user_regular_denied_no_permission(self):
        """Test that regular users cannot access webhook subscriptions in projects they don't have access to"""
        with self.assertRaises(PermissionDenied):
            get_webhook_subscription_for_user(self.regular_user_a, self.webhook_a2.object_id)

    def test_get_webhook_subscription_for_user_regular_denied_different_org(self):
        """Test that regular users cannot access webhook subscriptions in different organizations"""
        with self.assertRaises(Http404):
            get_webhook_subscription_for_user(self.regular_user_a, self.webhook_b1.object_id)

    # Tests for get_google_meet_bot_login_for_user()
    def test_get_google_meet_bot_login_for_user_admin_access_same_org(self):
        """Test that admin users can access any Google Meet bot login in their organization"""
        google_meet_bot_login = get_bot_login_for_user(self.admin_user_a, self.google_meet_bot_login_a1.object_id)
        self.assertEqual(google_meet_bot_login, self.google_meet_bot_login_a1)

        google_meet_bot_login = get_bot_login_for_user(self.admin_user_a, self.google_meet_bot_login_a2.object_id)
        self.assertEqual(google_meet_bot_login, self.google_meet_bot_login_a2)

    def test_get_google_meet_bot_login_for_user_admin_denied_different_org(self):
        """Test that admin users cannot access Google Meet bot logins in different organizations"""
        with self.assertRaises(Http404):
            get_bot_login_for_user(self.admin_user_a, self.google_meet_bot_login_b1.object_id)

    def test_get_google_meet_bot_login_for_user_regular_access_with_permission(self):
        """Test that regular users can access Google Meet bot logins in projects they have access to"""
        google_meet_bot_login = get_bot_login_for_user(self.regular_user_a, self.google_meet_bot_login_a1.object_id)
        self.assertEqual(google_meet_bot_login, self.google_meet_bot_login_a1)

    def test_get_google_meet_bot_login_for_user_regular_denied_no_permission(self):
        """Test that regular users cannot access Google Meet bot logins in projects they don't have access to"""
        with self.assertRaises(PermissionDenied):
            get_bot_login_for_user(self.regular_user_a, self.google_meet_bot_login_a2.object_id)

    def test_get_google_meet_bot_login_for_user_regular_denied_different_org(self):
        """Test that regular users cannot access Google Meet bot logins in different organizations"""
        with self.assertRaises(Http404):
            get_bot_login_for_user(self.regular_user_a, self.google_meet_bot_login_b1.object_id)

    # Tests for view-level access control through HTTP requests
    def test_project_dashboard_access_control(self):
        """Test that project dashboard access is properly controlled"""
        # Admin can access any project in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.get(reverse("bots:project-dashboard", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("bots:project-dashboard", kwargs={"object_id": self.project_a2.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only access projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.get(reverse("bots:project-dashboard", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot access projects they don't have access to
        response = self.client.get(reverse("bots:project-dashboard", kwargs={"object_id": self.project_a2.object_id}))
        self.assertEqual(response.status_code, 302)

        # Users cannot access projects in different organizations
        response = self.client.get(reverse("bots:project-dashboard", kwargs={"object_id": self.project_b1.object_id}))
        self.assertEqual(response.status_code, 302)

    def test_project_bots_access_control(self):
        """Test that project bots view access is properly controlled"""
        # Admin can access any project's bots in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.get(reverse("bots:project-bots", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("bots:project-bots", kwargs={"object_id": self.project_a2.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only access projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.get(reverse("bots:project-bots", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot access projects they don't have access to
        response = self.client.get(reverse("bots:project-bots", kwargs={"object_id": self.project_a2.object_id}))
        self.assertEqual(response.status_code, 403)

    def test_project_calendars_access_control(self):
        """Test that project calendars view access is properly controlled"""
        # Admin can access any project's calendars in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.get(reverse("bots:project-calendars", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("bots:project-calendars", kwargs={"object_id": self.project_a2.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only access projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.get(reverse("bots:project-calendars", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot access projects they don't have access to
        response = self.client.get(reverse("bots:project-calendars", kwargs={"object_id": self.project_a2.object_id}))
        self.assertEqual(response.status_code, 403)

    def test_bot_detail_access_control(self):
        """Test that bot detail view access is properly controlled"""
        # Admin can access any bot in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.get(reverse("bots:project-bot-detail", kwargs={"object_id": self.project_a1.object_id, "bot_object_id": self.bot_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("bots:project-bot-detail", kwargs={"object_id": self.project_a2.object_id, "bot_object_id": self.bot_a2.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only access bots in projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.get(reverse("bots:project-bot-detail", kwargs={"object_id": self.project_a1.object_id, "bot_object_id": self.bot_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot access bots in projects they don't have access to
        response = self.client.get(reverse("bots:project-bot-detail", kwargs={"object_id": self.project_a2.object_id, "bot_object_id": self.bot_a2.object_id}))
        self.assertEqual(response.status_code, 403)

    def test_calendar_detail_access_control(self):
        """Test that calendar detail view access is properly controlled"""
        # Admin can access any calendar in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.get(reverse("bots:project-calendar-detail", kwargs={"object_id": self.project_a1.object_id, "calendar_object_id": self.calendar_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("bots:project-calendar-detail", kwargs={"object_id": self.project_a2.object_id, "calendar_object_id": self.calendar_a2.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only access calendars in projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.get(reverse("bots:project-calendar-detail", kwargs={"object_id": self.project_a1.object_id, "calendar_object_id": self.calendar_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot access calendars in projects they don't have access to
        response = self.client.get(reverse("bots:project-calendar-detail", kwargs={"object_id": self.project_a2.object_id, "calendar_object_id": self.calendar_a2.object_id}))
        self.assertEqual(response.status_code, 302)  # Redirects to project calendars

    def test_calendar_event_detail_access_control(self):
        """Test that calendar event detail view access is properly controlled"""
        # Admin can access any calendar event in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.get(reverse("bots:project-calendar-event-detail", kwargs={"object_id": self.project_a1.object_id, "calendar_object_id": self.calendar_a1.object_id, "event_object_id": self.calendar_event_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only access calendar events in projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.get(reverse("bots:project-calendar-event-detail", kwargs={"object_id": self.project_a1.object_id, "calendar_object_id": self.calendar_a1.object_id, "event_object_id": self.calendar_event_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot access calendar events in projects they don't have access to
        response = self.client.get(reverse("bots:project-calendar-event-detail", kwargs={"object_id": self.project_a2.object_id, "calendar_object_id": self.calendar_a2.object_id, "event_object_id": self.calendar_event_a2.object_id}))
        self.assertEqual(response.status_code, 403)

    def test_api_key_deletion_access_control(self):
        """Test that API key deletion is properly controlled"""
        # Admin can delete any API key in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.delete(reverse("bots:delete-api-key", kwargs={"object_id": self.project_a1.object_id, "key_object_id": self.api_key_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only delete API keys in projects they have access to
        self.client.force_login(self.regular_user_a)
        # First recreate the API key since it was deleted above
        api_key_a1_new, _ = ApiKey.create(project=self.project_a1, name="API Key A1 New")
        response = self.client.delete(reverse("bots:delete-api-key", kwargs={"object_id": self.project_a1.object_id, "key_object_id": api_key_a1_new.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot delete API keys in projects they don't have access to
        response = self.client.delete(reverse("bots:delete-api-key", kwargs={"object_id": self.project_a2.object_id, "key_object_id": self.api_key_a2.object_id}))
        self.assertEqual(response.status_code, 403)

    def test_webhook_deletion_access_control(self):
        """Test that webhook deletion is properly controlled"""
        # Admin can delete any webhook in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.delete(reverse("bots:delete-webhook", kwargs={"object_id": self.project_a1.object_id, "webhook_object_id": self.webhook_a1.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user can only delete webhooks in projects they have access to
        self.client.force_login(self.regular_user_a)
        # First recreate the webhook since it was deleted above
        webhook_a1_new = WebhookSubscription.objects.create(project=self.project_a1, url="https://example.com/webhook/a1/new", triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE])
        response = self.client.delete(reverse("bots:delete-webhook", kwargs={"object_id": self.project_a1.object_id, "webhook_object_id": webhook_a1_new.object_id}))
        self.assertEqual(response.status_code, 200)

        # Regular user cannot delete webhooks in projects they don't have access to
        response = self.client.delete(reverse("bots:delete-webhook", kwargs={"object_id": self.project_a2.object_id, "webhook_object_id": self.webhook_a2.object_id}))
        self.assertEqual(response.status_code, 403)

    def test_cross_project_object_access_protection(self):
        """Test that objects from one project cannot be accessed via another project's URL"""
        self.client.force_login(self.admin_user_a)

        # Try to access project_a2's bot via project_a1's URL - should redirect or fail
        response = self.client.get(reverse("bots:project-bot-detail", kwargs={"object_id": self.project_a1.object_id, "bot_object_id": self.bot_a2.object_id}))
        # This should redirect to the correct project's bot list since the bot doesn't belong to project_a1
        self.assertEqual(response.status_code, 302)

        # Try to access project_a2's calendar via project_a1's URL
        response = self.client.get(reverse("bots:project-calendar-detail", kwargs={"object_id": self.project_a1.object_id, "calendar_object_id": self.calendar_a2.object_id}))
        # This should redirect since the calendar doesn't belong to project_a1
        self.assertEqual(response.status_code, 302)

        # Try to access project_a2's calendar event via project_a1's URL
        response = self.client.get(
            reverse(
                "bots:project-calendar-event-detail",
                kwargs={
                    "object_id": self.project_a1.object_id,
                    "calendar_object_id": self.calendar_a1.object_id,  # correct calendar
                    "event_object_id": self.calendar_event_a2.object_id,  # wrong event (from different calendar)
                },
            )
        )
        # This should redirect since the event doesn't belong to the specified calendar
        self.assertEqual(response.status_code, 302)

    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_zoom_oauth_app_creation_access_control(self, mock_client_id_validation):
        """Test that ZoomOAuthApp creation is properly controlled"""
        # Mock the validation to always return True
        mock_client_id_validation.return_value = True

        # Admin can create/update Zoom OAuth apps in any project in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.post(
            reverse("bots:create-zoom-oauth-app", kwargs={"object_id": self.project_a1.object_id}),
            data={"client_id": "new_client_id_a1", "client_secret": "new_secret_a1", "webhook_secret": "new_webhook_secret_a1"},
        )
        # Should succeed (200) or redirect (302)
        self.assertIn(response.status_code, [200, 302])

        response = self.client.post(
            reverse("bots:create-zoom-oauth-app", kwargs={"object_id": self.project_a2.object_id}),
            data={"client_id": "new_client_id_a2", "client_secret": "new_secret_a2", "webhook_secret": "new_webhook_secret_a2"},
        )
        self.assertIn(response.status_code, [200, 302])

        # Regular user can create/update Zoom OAuth apps in projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.post(
            reverse("bots:create-zoom-oauth-app", kwargs={"object_id": self.project_a1.object_id}),
            data={"client_secret": "updated_secret_a1"},  # Updating existing app
        )
        self.assertIn(response.status_code, [200, 302])

        # Regular user cannot create/update Zoom OAuth apps in projects they don't have access to
        response = self.client.post(
            reverse("bots:create-zoom-oauth-app", kwargs={"object_id": self.project_a2.object_id}),
            data={"client_id": "unauthorized_client_id", "client_secret": "unauthorized_secret"},
        )
        self.assertEqual(response.status_code, 403)

        # Users cannot create/update Zoom OAuth apps in different organizations
        response = self.client.post(
            reverse("bots:create-zoom-oauth-app", kwargs={"object_id": self.project_b1.object_id}),
            data={"client_id": "cross_org_client_id", "client_secret": "cross_org_secret"},
        )
        self.assertEqual(response.status_code, 404)

    def test_zoom_oauth_app_deletion_access_control(self):
        """Test that ZoomOAuthApp deletion is properly controlled"""
        # Admin can delete Zoom OAuth apps in any project in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.post(reverse("bots:delete-zoom-oauth-app", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)
        # Verify the app was deleted
        self.assertFalse(ZoomOAuthApp.objects.filter(project=self.project_a1).exists())

        # Recreate the app for further testing
        self.zoom_oauth_app_a1 = ZoomOAuthApp.objects.create(project=self.project_a1, client_id="test_client_id_a1")
        self.zoom_oauth_app_a1.set_credentials({"client_secret": "test_secret_a1", "webhook_secret": "test_webhook_secret_a1"})

        # Regular user can delete Zoom OAuth apps in projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.post(reverse("bots:delete-zoom-oauth-app", kwargs={"object_id": self.project_a1.object_id}))
        self.assertEqual(response.status_code, 200)
        # Verify the app was deleted
        self.assertFalse(ZoomOAuthApp.objects.filter(project=self.project_a1).exists())

        # Regular user cannot delete Zoom OAuth apps in projects they don't have access to
        response = self.client.post(reverse("bots:delete-zoom-oauth-app", kwargs={"object_id": self.project_a2.object_id}))
        self.assertEqual(response.status_code, 403)
        # Verify the app still exists
        self.assertTrue(ZoomOAuthApp.objects.filter(project=self.project_a2).exists())

        # Users cannot delete Zoom OAuth apps in different organizations
        response = self.client.post(reverse("bots:delete-zoom-oauth-app", kwargs={"object_id": self.project_b1.object_id}))
        self.assertEqual(response.status_code, 404)
        # Verify the app still exists
        self.assertTrue(ZoomOAuthApp.objects.filter(project=self.project_b1).exists())

    def test_bot_login_group_creation_access_control(self):
        """Test that bot login group creation is properly controlled"""
        self.client.force_login(self.admin_user_a)
        response = self.client.post(
            reverse("bots:create-bot-login-group", kwargs={"object_id": self.project_a1.object_id}),
            data={"platform": BotLoginPlatform.GOOGLE_MEET, "name": "Admin Created Group"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            BotLoginGroup.objects.filter(
                project=self.project_a1,
                platform=BotLoginPlatform.GOOGLE_MEET,
                name="Admin Created Group",
            ).exists()
        )

        self.client.force_login(self.regular_user_a)
        response = self.client.post(
            reverse("bots:create-bot-login-group", kwargs={"object_id": self.project_a1.object_id}),
            data={"platform": BotLoginPlatform.TEAMS, "name": "Regular User Group"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            BotLoginGroup.objects.filter(
                project=self.project_a1,
                platform=BotLoginPlatform.TEAMS,
                name="Regular User Group",
            ).exists()
        )

        response = self.client.post(
            reverse("bots:create-bot-login-group", kwargs={"object_id": self.project_a2.object_id}),
            data={"platform": BotLoginPlatform.TEAMS, "name": "Unauthorized Group"},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            reverse("bots:create-bot-login-group", kwargs={"object_id": self.project_b1.object_id}),
            data={"platform": BotLoginPlatform.TEAMS, "name": "Cross Org Group"},
        )
        self.assertEqual(response.status_code, 404)

    def test_bot_login_group_edit_access_control(self):
        """Test that bot login group editing is properly controlled"""
        self.client.force_login(self.admin_user_a)
        response = self.client.post(
            reverse(
                "bots:edit-bot-login-group",
                kwargs={
                    "object_id": self.project_a1.object_id,
                    "bot_login_group_object_id": self.google_meet_bot_login_group_a1.object_id,
                },
            ),
            data={"name": "Renamed Group"},
        )
        self.assertEqual(response.status_code, 200)
        self.google_meet_bot_login_group_a1.refresh_from_db()
        self.assertEqual(self.google_meet_bot_login_group_a1.name, "Renamed Group")

        self.client.force_login(self.regular_user_a)
        response = self.client.post(
            reverse(
                "bots:edit-bot-login-group",
                kwargs={
                    "object_id": self.project_a2.object_id,
                    "bot_login_group_object_id": self.google_meet_bot_login_group_a2.object_id,
                },
            ),
            data={"name": "Unauthorized Rename"},
        )
        self.assertEqual(response.status_code, 403)
        self.google_meet_bot_login_group_a2.refresh_from_db()
        self.assertEqual(self.google_meet_bot_login_group_a2.name, "Google Meet Group 1 A2")

    def test_bot_login_group_deletion_access_control(self):
        """Test that bot login group deletion is properly controlled"""
        self.client.force_login(self.admin_user_a)
        response = self.client.post(
            reverse(
                "bots:delete-bot-login-group",
                kwargs={
                    "object_id": self.project_a1.object_id,
                    "bot_login_group_object_id": self.teams_bot_login_group_a1.object_id,
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BotLoginGroup.objects.filter(id=self.teams_bot_login_group_a1.id).exists())

        self.teams_bot_login_group_a1 = BotLoginGroup.objects.create(
            project=self.project_a1,
            platform=BotLoginPlatform.TEAMS,
            name="Teams Group 1 A1 Recreated",
        )

        self.client.force_login(self.regular_user_a)
        response = self.client.post(
            reverse(
                "bots:delete-bot-login-group",
                kwargs={
                    "object_id": self.project_a2.object_id,
                    "bot_login_group_object_id": self.teams_bot_login_group_a2.object_id,
                },
            )
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(BotLoginGroup.objects.filter(id=self.teams_bot_login_group_a2.id).exists())
        

    def test_google_meet_bot_login_creation_access_control(self):
        """Test that Google Meet bot login creation is properly controlled"""
        # Admin can create Google Meet bot logins in any project in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.post(
            reverse("bots:create-google-meet-bot-login", kwargs={"object_id": self.project_a1.object_id}),
            data={
                "bot_login_group_object_id": self.google_meet_bot_login_group_a1.object_id,
                "workspace_domain": "new-workspace-a1.com",
                "email": "new-bot@new-workspace-a1.com",
                "private_key": "new_private_key",
                "cert": "new_cert",
            },
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse("bots:create-google-meet-bot-login", kwargs={"object_id": self.project_a2.object_id}),
            data={
                "bot_login_group_object_id": self.google_meet_bot_login_group_a2.object_id,
                "workspace_domain": "new-workspace-a2.com",
                "email": "new-bot@new-workspace-a2.com",
                "private_key": "new_private_key",
                "cert": "new_cert",
            },
        )
        self.assertEqual(response.status_code, 200)

        # Assert that expected objects in the database are created
        self.assertTrue(BotLoginGroup.objects.filter(project=self.project_a1, platform=BotLoginPlatform.GOOGLE_MEET).exists())
        self.assertTrue(BotLogin.objects.filter(group=self.google_meet_bot_login_group_a1).exists())
        self.assertTrue(BotLogin.objects.filter(group=self.google_meet_bot_login_group_a2).exists())
        self.assertTrue(BotLogin.objects.filter(group=self.google_meet_bot_login_group_b1).exists())
        self.assertEqual(BotLoginGroup.objects.get(id=self.google_meet_bot_login_group_a1.id).name, "Google Meet Group 1 A1")
        self.assertEqual(BotLoginGroup.objects.get(id=self.google_meet_bot_login_group_a2.id).name, "Google Meet Group 1 A2")
        self.assertEqual(BotLoginGroup.objects.get(id=self.google_meet_bot_login_group_b1.id).name, "Google Meet Group 1 B1")

        # Regular user can create Google Meet bot logins in projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.post(
            reverse("bots:create-google-meet-bot-login", kwargs={"object_id": self.project_a1.object_id}),
            data={
                "bot_login_group_object_id": self.google_meet_bot_login_group_a1.object_id,
                "workspace_domain": "another-workspace-a1.com",
                "email": "another-bot@another-workspace-a1.com",
                "private_key": "another_private_key",
                "cert": "another_cert",
            },
        )
        self.assertEqual(response.status_code, 200)

        # Regular user cannot create Google Meet bot logins in projects they don't have access to
        response = self.client.post(
            reverse("bots:create-google-meet-bot-login", kwargs={"object_id": self.project_a2.object_id}),
            data={
                "bot_login_group_object_id": self.google_meet_bot_login_group_a2.object_id,
                "workspace_domain": "unauthorized-workspace.com",
                "email": "unauthorized-bot@unauthorized-workspace.com",
                "private_key": "unauthorized_private_key",
                "cert": "unauthorized_cert",
            },
        )
        self.assertEqual(response.status_code, 403)

        # Users cannot create Google Meet bot logins in different organizations
        response = self.client.post(
            reverse("bots:create-google-meet-bot-login", kwargs={"object_id": self.project_b1.object_id}),
            data={
                "bot_login_group_object_id": self.google_meet_bot_login_group_b1.object_id,
                "workspace_domain": "cross-org-workspace.com",
                "email": "cross-org-bot@cross-org-workspace.com",
                "private_key": "cross_org_private_key",
                "cert": "cross_org_cert",
            },
        )
        self.assertEqual(response.status_code, 404)
    def test_teams_bot_login_creation_access_control(self):
        """Test that Teams bot login creation is properly controlled"""
        # Admin can create Teams bot logins in any project in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.post(
            reverse("bots:create-teams-bot-login", kwargs={"object_id": self.project_a1.object_id}),
            data={
                "bot_login_group_object_id": self.teams_bot_login_group_a1.object_id,
                "email": "new-teams-a1@example.com",
                "password": "new_password_a1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            BotLogin.objects.filter(
                group=self.teams_bot_login_group_a1,
                email="new-teams-a1@example.com",
            ).exists()
        )

        # Regular user cannot create Teams bot logins in projects they don't have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.post(
            reverse("bots:create-teams-bot-login", kwargs={"object_id": self.project_a2.object_id}),
            data={
                "bot_login_group_object_id": self.teams_bot_login_group_a2.object_id,
                "email": "unauthorized@example.com",
                "password": "unauthorized_password",
            },
        )
        self.assertEqual(response.status_code, 403)

        # Users cannot create Teams bot logins in different organizations
        response = self.client.post(
            reverse("bots:create-teams-bot-login", kwargs={"object_id": self.project_b1.object_id}),
            data={
                "bot_login_group_object_id": self.teams_bot_login_group_b1.object_id,
                "email": "cross-org-teams@example.com",
                "password": "cross_org_password",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_bot_login_deletion_access_control(self):
        """Test that bot login (generic) deletion is properly controlled"""
        # Admin can delete bot logins in any project in their org
        self.client.force_login(self.admin_user_a)
        response = self.client.post(
            reverse(
                "bots:delete-bot-login",
                kwargs={"object_id": self.project_a1.object_id, "bot_login_object_id": self.google_meet_bot_login_a1.object_id},
            )
        )
        self.assertEqual(response.status_code, 200)
        # Verify the login was deleted
        self.assertFalse(BotLogin.objects.filter(id=self.google_meet_bot_login_a1.id).exists())

        # Recreate the login for further testing
        self.google_meet_bot_login_a1 = BotLogin.objects.create(
            group=self.google_meet_bot_login_group_a1,
            workspace_domain="workspace-a1.com",
            email="bot-a1@workspace-a1.com",
        )
        self.google_meet_bot_login_a1.set_credentials({"private_key": "test_private_key_a1", "cert": "test_cert_a1"})

        # Regular user can delete bot logins in projects they have access to
        self.client.force_login(self.regular_user_a)
        response = self.client.post(
            reverse(
                "bots:delete-bot-login",
                kwargs={"object_id": self.project_a1.object_id, "bot_login_object_id": self.google_meet_bot_login_a1.object_id},
            )
        )
        self.assertEqual(response.status_code, 200)
        # Verify the login was deleted
        self.assertFalse(BotLogin.objects.filter(id=self.google_meet_bot_login_a1.id).exists())

        # Regular user cannot delete bot logins in projects they don't have access to
        response = self.client.post(
            reverse(
                "bots:delete-bot-login",
                kwargs={"object_id": self.project_a2.object_id, "bot_login_object_id": self.google_meet_bot_login_a2.object_id},
            )
        )
        self.assertEqual(response.status_code, 403)
        # Verify the login still exists
        self.assertTrue(BotLogin.objects.filter(id=self.google_meet_bot_login_a2.id).exists())

        # Users cannot delete bot logins in different organizations
        response = self.client.post(
            reverse(
                "bots:delete-bot-login",
                kwargs={"object_id": self.project_b1.object_id, "bot_login_object_id": self.google_meet_bot_login_b1.object_id},
            )
        )
        self.assertEqual(response.status_code, 404)
        # Verify the login still exists
        self.assertTrue(BotLogin.objects.filter(id=self.google_meet_bot_login_b1.id).exists())

    def test_unauthenticated_access_redirects_to_login(self):
        """Test that unauthenticated users are redirected to login"""
        # Test various endpoints without authentication
        test_urls = [
            reverse("bots:project-dashboard", kwargs={"object_id": self.project_a1.object_id}),
            reverse("bots:project-bots", kwargs={"object_id": self.project_a1.object_id}),
            reverse("bots:project-calendars", kwargs={"object_id": self.project_a1.object_id}),
            reverse("bots:project-bot-detail", kwargs={"object_id": self.project_a1.object_id, "bot_object_id": self.bot_a1.object_id}),
            reverse("bots:project-calendar-detail", kwargs={"object_id": self.project_a1.object_id, "calendar_object_id": self.calendar_a1.object_id}),
        ]

        for url in test_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)
