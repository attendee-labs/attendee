from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.management.commands.run_scheduler import Command
from bots.models import Project, WebexServiceApp
from bots.tasks.refresh_webex_service_app_task import refresh_webex_service_app
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

WEBEX_CREDENTIALS = {"client_secret": "test_client_secret", "refresh_token": "test_refresh_token"}


class WebexServiceAppTestCase(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.webex_service_app = self.create_webex_service_app(self.project, tokens_refreshed_days_ago=8)

    def create_webex_service_app(self, project, tokens_refreshed_days_ago):
        tokens_refreshed_at = timezone.now() - timezone.timedelta(days=tokens_refreshed_days_ago) if tokens_refreshed_days_ago is not None else None
        webex_service_app = WebexServiceApp(project=project, client_id="test_client_id", service_app_id="test_service_app_id", org_id="test_org_id", tokens_refreshed_at=tokens_refreshed_at)
        webex_service_app.set_credentials(WEBEX_CREDENTIALS)
        return webex_service_app


class TestRefreshWebexServiceApp(WebexServiceAppTestCase):
    @patch("bots.tasks.refresh_webex_service_app_task.refresh_service_app_tokens")
    def test_refresh_saves_the_refresh_token_webex_returns(self, mock_refresh_service_app_tokens):
        mock_refresh_service_app_tokens.return_value = {"access_token": "test_access_token", "refresh_token": "new_refresh_token", "refresh_token_expires_in": 7776000}

        refresh_webex_service_app(self.webex_service_app.id)

        mock_refresh_service_app_tokens.assert_called_once_with("test_client_id", "test_client_secret", "test_refresh_token")
        self.webex_service_app.refresh_from_db()
        self.assertEqual(self.webex_service_app.get_credentials(), {**WEBEX_CREDENTIALS, "refresh_token": "new_refresh_token"})

    @patch("bots.tasks.refresh_webex_service_app_task.refresh_service_app_tokens")
    def test_rejected_refresh_token_keeps_the_credentials(self, mock_refresh_service_app_tokens):
        mock_refresh_service_app_tokens.side_effect = WebexAuthError("POST /access_token failed with 400: invalid refresh token")

        refresh_webex_service_app(self.webex_service_app.id)

        self.webex_service_app.refresh_from_db()
        self.assertEqual(self.webex_service_app.get_credentials(), WEBEX_CREDENTIALS)


class TestRunPeriodicWebexServiceAppTokenRefreshs(WebexServiceAppTestCase):
    @patch("bots.management.commands.run_scheduler.enqueue_refresh_webex_service_app_task")
    def test_only_service_apps_not_refreshed_in_the_last_7_days_are_refreshed(self, mock_enqueue):
        recently_refreshed_project = Project.objects.create(name="Recently Refreshed Project", organization=self.organization)
        self.create_webex_service_app(recently_refreshed_project, tokens_refreshed_days_ago=2)

        Command()._run_periodic_webex_service_app_token_refreshs()

        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.args[0].id, self.webex_service_app.id)

    @patch("bots.management.commands.run_scheduler.enqueue_refresh_webex_service_app_task")
    def test_service_apps_that_are_not_authorized_are_not_refreshed(self, mock_enqueue):
        WebexServiceApp.objects.all().delete()
        self.create_webex_service_app(self.project, tokens_refreshed_days_ago=None)

        Command()._run_periodic_webex_service_app_token_refreshs()

        mock_enqueue.assert_not_called()

    @patch("bots.tasks.refresh_webex_service_app_task.refresh_webex_service_app.delay")
    def test_enqueued_service_apps_are_not_enqueued_again_in_the_next_cycle(self, mock_delay):
        Command()._run_periodic_webex_service_app_token_refreshs()
        Command()._run_periodic_webex_service_app_token_refreshs()

        mock_delay.assert_called_once_with(self.webex_service_app.id)
