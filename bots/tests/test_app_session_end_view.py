from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory

from bots import app_session_api_views as views


class AppSessionEndViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = views.AppSessionEndView.as_view()

    def _authed_request(self, payload):
        project = SimpleNamespace(object_id="proj_test", id=1)
        auth = SimpleNamespace(project=project)
        request = self.factory.post(
            "/api/v1/app_sessions/end",
            payload,
            format="json",
            HTTP_AUTHORIZATION="Token test",
        )
        request.auth = auth
        return request

    def test_missing_zoom_rtms_returns_400(self):
        with patch.object(views.ApiKeyAuthentication, "authenticate") as mock_auth:
            mock_auth.return_value = (None, SimpleNamespace(project=SimpleNamespace(object_id="proj_test", id=1)))
            response = self.view(self._authed_request({}))
            self.assertEqual(response.status_code, 400)
            self.assertIn("zoom_rtms.rtms_stream_id", response.data["error"])

    def test_null_zoom_rtms_returns_400(self):
        with patch.object(views.ApiKeyAuthentication, "authenticate") as mock_auth:
            mock_auth.return_value = (None, SimpleNamespace(project=SimpleNamespace(object_id="proj_test", id=1)))
            response = self.view(self._authed_request({"zoom_rtms": None}))
            self.assertEqual(response.status_code, 400)

    def test_missing_rtms_stream_id_returns_400(self):
        with patch.object(views.ApiKeyAuthentication, "authenticate") as mock_auth:
            mock_auth.return_value = (None, SimpleNamespace(project=SimpleNamespace(object_id="proj_test", id=1)))
            response = self.view(self._authed_request({"zoom_rtms": {}}))
            self.assertEqual(response.status_code, 400)
