from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.management.commands.run_scheduler import Command
from bots.models import Credentials, Project
from bots.tasks.refresh_webex_service_app_credentials_task import refresh_webex_service_app_credentials
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

WEBEX_CREDENTIALS = {"client_id": "test_client_id", "client_secret": "test_client_secret", "refresh_token": "test_refresh_token"}


class WebexServiceAppCredentialsTestCase(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.WEBEX_SERVICE_APP)
        self.credentials.set_credentials(WEBEX_CREDENTIALS)

    def set_last_refreshed_days_ago(self, credentials, days):
        # update() skips auto_now, so updated_at keeps the value we give it
        Credentials.objects.filter(id=credentials.id).update(updated_at=timezone.now() - timezone.timedelta(days=days))


class TestRefreshWebexServiceAppCredentials(WebexServiceAppCredentialsTestCase):
    @patch("bots.tasks.refresh_webex_service_app_credentials_task.refresh_service_app_tokens")
    def test_refresh_saves_the_refresh_token_webex_returns(self, mock_refresh_service_app_tokens):
        mock_refresh_service_app_tokens.return_value = {"access_token": "test_access_token", "refresh_token": "new_refresh_token", "refresh_token_expires_in": 7776000}
        self.set_last_refreshed_days_ago(self.credentials, 8)

        refresh_webex_service_app_credentials(self.credentials.id)

        mock_refresh_service_app_tokens.assert_called_once_with("test_client_id", "test_client_secret", "test_refresh_token")
        self.credentials.refresh_from_db()
        self.assertEqual(self.credentials.get_credentials(), {**WEBEX_CREDENTIALS, "refresh_token": "new_refresh_token"})
        self.assertGreater(self.credentials.updated_at, timezone.now() - timezone.timedelta(minutes=1))

    @patch("bots.tasks.refresh_webex_service_app_credentials_task.refresh_service_app_tokens")
    def test_rejected_refresh_token_keeps_the_credentials(self, mock_refresh_service_app_tokens):
        mock_refresh_service_app_tokens.side_effect = WebexAuthError("POST /access_token failed with 400: invalid refresh token")

        refresh_webex_service_app_credentials(self.credentials.id)

        self.credentials.refresh_from_db()
        self.assertEqual(self.credentials.get_credentials(), WEBEX_CREDENTIALS)


class TestRunPeriodicWebexServiceAppCredentialsTokenRefreshs(WebexServiceAppCredentialsTestCase):
    @patch("bots.management.commands.run_scheduler.enqueue_refresh_webex_service_app_credentials_task")
    def test_only_credentials_not_refreshed_in_the_last_7_days_are_refreshed(self, mock_enqueue):
        recently_refreshed_project = Project.objects.create(name="Recently Refreshed Project", organization=self.organization)
        recently_refreshed_credentials = Credentials.objects.create(project=recently_refreshed_project, credential_type=Credentials.CredentialTypes.WEBEX_SERVICE_APP)
        self.set_last_refreshed_days_ago(self.credentials, 8)
        self.set_last_refreshed_days_ago(recently_refreshed_credentials, 2)

        Command()._run_periodic_webex_service_app_credentials_token_refreshs()

        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.args[0].id, self.credentials.id)

    @patch("bots.management.commands.run_scheduler.enqueue_refresh_webex_service_app_credentials_task")
    def test_other_credential_types_are_not_refreshed(self, mock_enqueue):
        deepgram_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM)
        self.set_last_refreshed_days_ago(deepgram_credentials, 30)

        Command()._run_periodic_webex_service_app_credentials_token_refreshs()

        mock_enqueue.assert_not_called()

    @patch("bots.tasks.refresh_webex_service_app_credentials_task.refresh_webex_service_app_credentials.delay")
    def test_enqueued_credentials_are_not_enqueued_again_in_the_next_cycle(self, mock_delay):
        self.set_last_refreshed_days_ago(self.credentials, 8)

        Command()._run_periodic_webex_service_app_credentials_token_refreshs()
        Command()._run_periodic_webex_service_app_credentials_token_refreshs()

        mock_delay.assert_called_once_with(self.credentials.id)
