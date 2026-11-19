import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from bots.models import ApiKey, Organization, Project, WebexServiceApp
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

SERVICE_APP_FIELDS = {"client_id": "C1234", "client_secret": "secret-9876", "service_app_id": "app-5432", "org_id": "org-1111"}
ISSUED_TOKENS = {"access_token": "service-app-access-token", "refresh_token": "service-app-refresh-token", "refresh_token_expires_in": 7776000}
URL = "/api/v1/webex_service_app"
AUTHORIZE_URL = "/api/v1/webex_service_app/authorize"


@override_settings(WEBEX_INTEGRATION_CLIENT_ID="integration-id", WEBEX_INTEGRATION_CLIENT_SECRET="integration-secret")
class WebexServiceAppApiTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        _, self.api_key = ApiKey.create(project=self.project, name="Test API Key")
        self.client = Client()

    def request(self, method, url, data=None, api_key=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Token {api_key or self.api_key}"}
        if data is not None:
            kwargs.update(data=json.dumps(data), content_type="application/json")
        return getattr(self.client, method)(url, **kwargs)

    def set_service_app(self, fields=SERVICE_APP_FIELDS):
        return self.request("put", URL, fields)

    def authorize(self, data):
        return self.request("post", AUTHORIZE_URL, data)

    def test_get_without_a_service_app_is_404(self):
        response = self.request("get", URL)

        self.assertEqual(response.status_code, 404)

    def test_put_saves_the_service_app_with_the_secret_encrypted_and_never_returned(self):
        response = self.set_service_app()

        self.assertEqual(response.status_code, 200)
        webex_service_app = WebexServiceApp.objects.get(project=self.project)
        self.assertEqual((webex_service_app.client_id, webex_service_app.service_app_id, webex_service_app.org_id), ("C1234", "app-5432", "org-1111"))
        self.assertEqual(webex_service_app.get_credentials(), {"client_secret": "secret-9876"})
        self.assertNotIn(b"secret-9876", bytes(webex_service_app._encrypted_data))
        self.assertEqual(response.json()["id"], webex_service_app.object_id)
        self.assertFalse(response.json()["is_authorized"])
        self.assertIsNone(response.json()["tokens_refreshed_at"])
        self.assertNotIn("secret-9876", response.content.decode())

        response = self.request("get", URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_id"], "C1234")
        self.assertNotIn("secret-9876", response.content.decode())

    def test_put_needs_every_field_and_no_others(self):
        for missing_field in SERVICE_APP_FIELDS:
            with self.subTest(missing_field=missing_field):
                response = self.set_service_app({key: value for key, value in SERVICE_APP_FIELDS.items() if key != missing_field})
                self.assertEqual(response.status_code, 400)

        response = self.set_service_app({**SERVICE_APP_FIELDS, "refresh_token": "not-allowed"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WebexServiceApp.objects.exists())

    def test_put_replaces_the_service_app_and_its_authorization(self):
        self.set_service_app()
        WebexServiceApp.objects.get().set_credentials({"client_secret": "secret-9876", "refresh_token": "refresh-token"})

        response = self.set_service_app({**SERVICE_APP_FIELDS, "client_id": "C5678"})

        self.assertEqual(response.status_code, 200)
        webex_service_app = WebexServiceApp.objects.get(project=self.project)
        self.assertEqual(webex_service_app.client_id, "C5678")
        self.assertIsNone(webex_service_app.refresh_token)
        self.assertFalse(response.json()["is_authorized"])

    def test_delete_removes_the_service_app(self):
        self.set_service_app()

        response = self.request("delete", URL)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(WebexServiceApp.objects.exists())
        self.assertEqual(self.request("delete", URL).status_code, 404)

    @patch("bots.webex_service_app_api_views.exchange_integration_authorization_code")
    @patch("bots.webex_service_app_api_views.issue_service_app_tokens", return_value=ISSUED_TOKENS)
    def test_authorize_with_an_access_token_issues_and_saves_the_service_app_tokens(self, mock_issue, mock_exchange):
        self.set_service_app()

        response = self.authorize({"access_token": "creator-access-token"})

        self.assertEqual(response.status_code, 200)
        mock_exchange.assert_not_called()
        mock_issue.assert_called_once_with("app-5432", "C1234", "secret-9876", "org-1111", "creator-access-token")
        webex_service_app = WebexServiceApp.objects.get(project=self.project)
        self.assertEqual(webex_service_app.get_credentials(), {"client_secret": "secret-9876", "refresh_token": "service-app-refresh-token"})
        self.assertIsNotNone(webex_service_app.tokens_refreshed_at)
        self.assertTrue(response.json()["is_authorized"])
        for token in ("creator-access-token", "service-app-access-token", "service-app-refresh-token"):
            self.assertNotIn(token, response.content.decode())

    @patch("bots.webex_service_app_api_views.exchange_integration_authorization_code", return_value={"access_token": "integration-access-token"})
    @patch("bots.webex_service_app_api_views.issue_service_app_tokens", return_value=ISSUED_TOKENS)
    def test_authorize_with_an_authorization_code_exchanges_it_with_the_integration(self, mock_issue, mock_exchange):
        self.set_service_app()

        response = self.authorize({"authorization_code": "code-123", "redirect_uri": "https://app.example.com/webex/callback"})

        self.assertEqual(response.status_code, 200)
        mock_exchange.assert_called_once_with("integration-id", "integration-secret", "code-123", "https://app.example.com/webex/callback")
        mock_issue.assert_called_once_with("app-5432", "C1234", "secret-9876", "org-1111", "integration-access-token")
        self.assertEqual(WebexServiceApp.objects.get(project=self.project).refresh_token, "service-app-refresh-token")

    @override_settings(WEBEX_INTEGRATION_CLIENT_ID=None, WEBEX_INTEGRATION_CLIENT_SECRET=None)
    @patch("bots.webex_service_app_api_views.exchange_integration_authorization_code")
    def test_authorize_with_an_authorization_code_needs_the_integration(self, mock_exchange):
        self.set_service_app()

        response = self.authorize({"authorization_code": "code-123", "redirect_uri": "https://app.example.com/webex/callback"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("WEBEX_INTEGRATION_CLIENT_ID", response.json()["error"])
        mock_exchange.assert_not_called()

    @patch("bots.webex_service_app_api_views.issue_service_app_tokens")
    def test_authorize_needs_exactly_one_kind_of_token(self, mock_issue):
        self.set_service_app()
        invalid_requests = {
            "nothing": {},
            "both": {"authorization_code": "code-123", "redirect_uri": "https://app.example.com/webex/callback", "access_token": "token"},
            "code without redirect_uri": {"authorization_code": "code-123"},
            "redirect_uri with access_token": {"access_token": "token", "redirect_uri": "https://app.example.com/webex/callback"},
            "unexpected field": {"access_token": "token", "refresh_token": "token"},
        }
        for name, data in invalid_requests.items():
            with self.subTest(name):
                self.assertEqual(self.authorize(data).status_code, 400)

        mock_issue.assert_not_called()
        self.assertIsNone(WebexServiceApp.objects.get().refresh_token)

    @patch("bots.webex_service_app_api_views.issue_service_app_tokens", side_effect=WebexAuthError("POST /applications/app-5432/token failed with 401: unauthorized"))
    def test_authorize_reports_a_webex_failure_and_saves_nothing(self, mock_issue):
        self.set_service_app()

        response = self.authorize({"access_token": "expired-token"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("failed with 401", response.json()["error"])
        webex_service_app = WebexServiceApp.objects.get()
        self.assertIsNone(webex_service_app.refresh_token)
        self.assertIsNone(webex_service_app.tokens_refreshed_at)

    def test_authorize_without_a_service_app_is_404(self):
        self.assertEqual(self.authorize({"access_token": "token"}).status_code, 404)

    def test_requests_need_an_api_key(self):
        self.set_service_app()

        self.assertEqual(self.client.get(URL).status_code, 401)
        self.assertEqual(self.client.post(AUTHORIZE_URL, data=json.dumps({"access_token": "token"}), content_type="application/json").status_code, 401)

    @patch("bots.webex_service_app_api_views.issue_service_app_tokens", return_value=ISSUED_TOKENS)
    def test_another_projects_api_key_cannot_see_or_change_the_service_app(self, mock_issue):
        self.set_service_app()
        other_project = Project.objects.create(name="Other Project", organization=Organization.objects.create(name="Other Organization"))
        _, other_api_key = ApiKey.create(project=other_project, name="Other API Key")

        self.assertEqual(self.request("get", URL, api_key=other_api_key).status_code, 404)
        self.assertEqual(self.request("post", AUTHORIZE_URL, {"access_token": "token"}, api_key=other_api_key).status_code, 404)
        self.assertEqual(self.request("delete", URL, api_key=other_api_key).status_code, 404)
        mock_issue.assert_not_called()
        self.assertTrue(WebexServiceApp.objects.filter(project=self.project).exists())
