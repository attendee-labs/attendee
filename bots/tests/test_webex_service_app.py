from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from bots.models import Organization, Project, WebexServiceApp
from bots.projects_views import WEBEX_OAUTH_STATE_SALT
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

User = get_user_model()

SERVICE_APP_FIELDS = {"client_id": "C1234", "client_secret": "secret-9876", "service_app_id": "app-5432", "org_id": "org-1111"}
REDIRECT_URI = "http://localhost:8000/projects/webex-service-app/oauth-callback/"


@override_settings(WEBEX_INTEGRATION_CLIENT_ID="integration-id", WEBEX_INTEGRATION_CLIENT_SECRET="integration-secret")
class WebexServiceAppViewsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Organization")
        self.user = User.objects.create_user(username="testuser", email="test@example.com", password="testpass123", organization=self.organization)
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.client.force_login(self.user)

    def save_service_app(self, fields=SERVICE_APP_FIELDS):
        return self.client.post(reverse("projects:create-webex-service-app", args=[self.project.object_id]), fields)

    def authorize(self):
        response = self.client.get(reverse("projects:authorize-webex-service-app", args=[self.project.object_id]))
        return parse_qs(urlparse(response["Location"]).query)["state"][0]

    def callback(self, state, **params):
        return self.client.get(reverse("projects:webex-service-app-oauth-callback"), {"state": state, **params})

    def test_service_app_is_saved_with_the_secret_encrypted(self):
        response = self.save_service_app()

        self.assertEqual(response.status_code, 200)
        webex_service_app = WebexServiceApp.objects.get(project=self.project)
        self.assertEqual((webex_service_app.client_id, webex_service_app.service_app_id, webex_service_app.org_id), ("C1234", "app-5432", "org-1111"))
        self.assertEqual(webex_service_app.get_credentials(), {"client_secret": "secret-9876"})
        self.assertNotIn(b"secret-9876", bytes(webex_service_app._encrypted_data))
        self.assertContains(response, "Not authorized")
        self.assertNotContains(response, "secret-9876")

    def test_every_field_is_required(self):
        for missing_field in SERVICE_APP_FIELDS:
            with self.subTest(missing_field=missing_field):
                response = self.save_service_app({key: value for key, value in SERVICE_APP_FIELDS.items() if key != missing_field})

                self.assertEqual(response.status_code, 400)
                self.assertFalse(WebexServiceApp.objects.exists())

    def test_saving_again_replaces_the_service_app_and_its_authorization(self):
        self.save_service_app()
        WebexServiceApp.objects.get().set_credentials({"client_secret": "secret-9876", "refresh_token": "refresh-token"})

        self.save_service_app({**SERVICE_APP_FIELDS, "client_id": "C5678"})

        webex_service_app = WebexServiceApp.objects.get(project=self.project)
        self.assertEqual(webex_service_app.client_id, "C5678")
        self.assertIsNone(webex_service_app.refresh_token)

    def test_service_app_can_be_deleted(self):
        self.save_service_app()

        response = self.client.post(reverse("projects:delete-webex-service-app", args=[self.project.object_id]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(WebexServiceApp.objects.exists())
        self.assertContains(response, "Add Service App needed to launch Webex Bots.")

    def test_authorize_redirects_to_the_webex_sign_in_page(self):
        self.save_service_app()

        response = self.client.get(reverse("projects:authorize-webex-service-app", args=[self.project.object_id]))

        location = urlparse(response["Location"])
        query = parse_qs(location.query)
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", "https://webexapis.com/v1/authorize")
        self.assertEqual(query["client_id"], ["integration-id"])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["scope"], ["spark:applications_token"])
        self.assertEqual(signing.loads(query["state"][0], salt=WEBEX_OAUTH_STATE_SALT), {"project_object_id": self.project.object_id, "user_id": self.user.id})

    @override_settings(WEBEX_INTEGRATION_CLIENT_ID=None)
    def test_authorize_needs_the_integration_to_be_configured(self):
        self.save_service_app()

        response = self.client.get(reverse("projects:authorize-webex-service-app", args=[self.project.object_id]))

        self.assertEqual(response.status_code, 400)
        page = self.client.get(reverse("projects:project-credentials", args=[self.project.object_id]))
        self.assertContains(page, "Set WEBEX_INTEGRATION_CLIENT_ID and WEBEX_INTEGRATION_CLIENT_SECRET")

    @patch("bots.projects_views.issue_service_app_tokens", return_value={"access_token": "service-app-access-token", "refresh_token": "service-app-refresh-token"})
    @patch("bots.projects_views.exchange_integration_authorization_code", return_value={"access_token": "integration-access-token"})
    def test_callback_saves_the_service_app_refresh_token(self, exchange_code, issue_tokens):
        self.save_service_app()

        response = self.callback(self.authorize(), code="auth-code")

        self.assertRedirects(response, reverse("projects:project-credentials", args=[self.project.object_id]), fetch_redirect_response=False)
        exchange_code.assert_called_once_with("integration-id", "integration-secret", "auth-code", REDIRECT_URI)
        issue_tokens.assert_called_once_with("app-5432", "C1234", "secret-9876", "org-1111", "integration-access-token")
        webex_service_app = WebexServiceApp.objects.get(project=self.project)
        self.assertEqual(webex_service_app.refresh_token, "service-app-refresh-token")
        self.assertIsNotNone(webex_service_app.tokens_refreshed_at)

    @patch("bots.projects_views.issue_service_app_tokens", side_effect=WebexAuthError("POST /applications/app-5432/token failed with 403"))
    @patch("bots.projects_views.exchange_integration_authorization_code", return_value={"access_token": "integration-access-token"})
    def test_callback_shows_the_failure_when_webex_rejects_the_service_app(self, exchange_code, issue_tokens):
        self.save_service_app()

        response = self.callback(self.authorize(), code="auth-code")

        self.assertEqual(response["Location"], reverse("projects:project-credentials", args=[self.project.object_id]) + "?webex_service_app_authorization_failed")
        self.assertIsNone(WebexServiceApp.objects.get(project=self.project).refresh_token)
        self.assertContains(self.client.get(response["Location"]), "Webex authorization failed.")

    @patch("bots.projects_views.exchange_integration_authorization_code")
    def test_callback_shows_the_failure_when_the_user_declines(self, exchange_code):
        self.save_service_app()

        response = self.callback(self.authorize(), error="access_denied")

        self.assertTrue(response["Location"].endswith("?webex_service_app_authorization_failed"))
        exchange_code.assert_not_called()

    @patch("bots.projects_views.exchange_integration_authorization_code")
    def test_callback_rejects_a_state_that_was_not_signed_by_attendee(self, exchange_code):
        self.save_service_app()

        response = self.callback(signing.dumps({"project_object_id": self.project.object_id, "user_id": self.user.id}, salt="another-salt"), code="auth-code")

        self.assertEqual(response.status_code, 400)
        exchange_code.assert_not_called()

    @patch("bots.projects_views.exchange_integration_authorization_code")
    def test_callback_rejects_a_state_started_by_another_user(self, exchange_code):
        self.save_service_app()
        state = self.authorize()
        other_user = User.objects.create_user(username="otheruser", email="other@example.com", password="testpass123", organization=self.organization)
        self.client.force_login(other_user)

        response = self.callback(state, code="auth-code")

        self.assertEqual(response.status_code, 403)
        exchange_code.assert_not_called()
