import json
from unittest.mock import patch

from django.test import Client, TransactionTestCase
from rest_framework import status

from accounts.models import Organization
from bots.models import (
    ApiKey,
    Project,
    ZoomOAuthApp,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)


class ZoomOAuthConnectionsApiObjectAccessIntegrationTest(TransactionTestCase):
    """Integration tests for API object access control in zoom_oauth_connections_api_views.py"""

    def setUp(self):
        """Set up test environment with multiple organizations, projects, and API keys"""

        # Create two organizations
        self.organization_a = Organization.objects.create(name="Organization A")
        self.organization_b = Organization.objects.create(name="Organization B")

        # Create projects in each organization
        self.project_a = Project.objects.create(name="Project A", organization=self.organization_a)
        self.project_b = Project.objects.create(name="Project B", organization=self.organization_b)

        # Create API keys for each project
        self.api_key_a, self.api_key_a_plain = ApiKey.create(project=self.project_a, name="API Key A")
        self.api_key_b, self.api_key_b_plain = ApiKey.create(project=self.project_b, name="API Key B")

        # Create test objects for access testing
        self._create_test_objects()

        # Create test client
        self.client = Client()

    def _create_test_objects(self):
        """Create test objects (zoom oauth apps, zoom oauth connections) for access testing"""

        # Create zoom oauth apps in each project
        self.zoom_oauth_app_a = ZoomOAuthApp.objects.create(project=self.project_a, client_id="client_id_a")
        self.zoom_oauth_app_a.set_credentials({"client_secret": "secret_a", "webhook_secret": "webhook_secret_a"})

        self.zoom_oauth_app_b = ZoomOAuthApp.objects.create(project=self.project_b, client_id="client_id_b")
        self.zoom_oauth_app_b.set_credentials({"client_secret": "secret_b", "webhook_secret": "webhook_secret_b"})

        # Create zoom oauth connections in each project
        self.zoom_oauth_connection_a = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app_a,
            user_id="user_id_a",
            account_id="account_id_a",
            state=ZoomOAuthConnectionStates.CONNECTED,
            metadata={"tenant_id": "tenant_a"},
        )
        self.zoom_oauth_connection_a.set_credentials({"refresh_token": "refresh_token_a"})

        self.zoom_oauth_connection_b = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app_b,
            user_id="user_id_b",
            account_id="account_id_b",
            state=ZoomOAuthConnectionStates.CONNECTED,
            metadata={"tenant_id": "tenant_b"},
        )
        self.zoom_oauth_connection_b.set_credentials({"refresh_token": "refresh_token_b"})

    def _make_authenticated_request(self, method, url, api_key, data=None):
        """Helper method to make authenticated API requests"""
        headers = {"HTTP_AUTHORIZATION": f"Token {api_key}", "HTTP_CONTENT_TYPE": "application/json"}

        if method.upper() == "GET":
            return self.client.get(url, **headers)
        elif method.upper() == "POST":
            return self.client.post(url, data=data, content_type="application/json", **headers)
        elif method.upper() == "PATCH":
            return self.client.patch(url, data=data, content_type="application/json", **headers)
        elif method.upper() == "DELETE":
            return self.client.delete(url, **headers)

    # Tests for Zoom OAuth Connection List View (GET /api/zoom_oauth_connections)
    def test_zoom_oauth_connection_list_access_control(self):
        """Test that zoom oauth connection list only returns connections from the authenticated project"""
        # API key A can only see zoom oauth connections from project A
        response = self._make_authenticated_request("GET", "/api/v1/zoom_oauth_connections", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", response.json())
        if isinstance(results, list):
            # Should only see zoom_oauth_connection_a, not zoom_oauth_connection_b
            connection_ids = [conn["id"] for conn in results]
            self.assertIn(self.zoom_oauth_connection_a.object_id, connection_ids)
            self.assertNotIn(self.zoom_oauth_connection_b.object_id, connection_ids)

        # API key B can only see zoom oauth connections from project B
        response = self._make_authenticated_request("GET", "/api/v1/zoom_oauth_connections", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", response.json())
        if isinstance(results, list):
            # Should only see zoom_oauth_connection_b, not zoom_oauth_connection_a
            connection_ids = [conn["id"] for conn in results]
            self.assertIn(self.zoom_oauth_connection_b.object_id, connection_ids)
            self.assertNotIn(self.zoom_oauth_connection_a.object_id, connection_ids)

    # Tests for Zoom OAuth Connection Create View (POST /api/zoom_oauth_connections)
    @patch("bots.zoom_oauth_connections_api_utils._exchange_access_code_for_tokens")
    @patch("bots.zoom_oauth_connections_api_utils._get_user_info")
    @patch("bots.tasks.sync_zoom_oauth_connection_task.enqueue_sync_zoom_oauth_connection_task")
    def test_zoom_oauth_connection_create_uses_correct_project(self, mock_enqueue_sync, mock_get_user_info, mock_exchange_tokens):
        """Test that zoom oauth connection creation uses the zoom oauth app from the correct project"""
        # Mock the Zoom API calls
        mock_exchange_tokens.return_value = {
            "access_token": "access_token_new",
            "refresh_token": "refresh_token_new",
            "scope": "user:read:user user:read:zak meeting:read:list_meetings meeting:read:local_recording_token",
        }
        mock_get_user_info.return_value = {"id": "new_user_id", "account_id": "new_account_id", "status": "active"}

        connection_data = {
            "zoom_oauth_app_id": self.zoom_oauth_app_a.object_id,
            "authorization_code": "auth_code_123",
            "redirect_uri": "https://example.com/callback",
            "metadata": {"tenant_id": "new_tenant"},
        }

        response = self._make_authenticated_request("POST", "/api/v1/zoom_oauth_connections", self.api_key_a_plain, json.dumps(connection_data))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created_connection = ZoomOAuthConnection.objects.get(object_id=response.json()["id"])
        self.assertEqual(created_connection.zoom_oauth_app, self.zoom_oauth_app_a)
        self.assertEqual(created_connection.zoom_oauth_app.project, self.project_a)

    @patch("bots.zoom_oauth_connections_api_utils._exchange_access_code_for_tokens")
    @patch("bots.zoom_oauth_connections_api_utils._get_user_info")
    @patch("bots.tasks.sync_zoom_oauth_connection_task.enqueue_sync_zoom_oauth_connection_task")
    def test_zoom_oauth_connection_create_cannot_use_other_project_zoom_oauth_app(self, mock_enqueue_sync, mock_get_user_info, mock_exchange_tokens):
        """Test that zoom oauth connection creation cannot use a zoom oauth app from another project"""
        # Mock the Zoom API calls
        mock_exchange_tokens.return_value = {
            "access_token": "access_token_new",
            "refresh_token": "refresh_token_new",
            "scope": "user:read:user user:read:zak meeting:read:list_meetings meeting:read:local_recording_token",
        }
        mock_get_user_info.return_value = {"id": "new_user_id", "account_id": "new_account_id", "status": "active"}

        # Try to create a zoom oauth connection using zoom_oauth_app_b with api_key_a
        connection_data = {
            "zoom_oauth_app_id": self.zoom_oauth_app_b.object_id,
            "authorization_code": "auth_code_123",
            "redirect_uri": "https://example.com/callback",
            "metadata": {"tenant_id": "new_tenant"},
        }

        response = self._make_authenticated_request("POST", "/api/v1/zoom_oauth_connections", self.api_key_a_plain, json.dumps(connection_data))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not exist in this project", response.json()["error"])

    # Tests for Zoom OAuth Connection Detail View (GET /api/zoom_oauth_connections/<object_id>)
    def test_zoom_oauth_connection_detail_access_control(self):
        """Test that API key can only access zoom oauth connections in its own project"""
        # API key A can access zoom_oauth_connection_a
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], self.zoom_oauth_connection_a.object_id)

        # API key A cannot access zoom_oauth_connection_b
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_b.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # API key B can access zoom_oauth_connection_b
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_b.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], self.zoom_oauth_connection_b.object_id)

        # API key B cannot access zoom_oauth_connection_a
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_a.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Zoom OAuth Connection Delete View (DELETE /api/zoom_oauth_connections/<object_id>)
    def test_zoom_oauth_connection_delete_access_control(self):
        """Test that DELETE requests respect project boundaries"""
        # Create additional zoom oauth connections for deletion
        connection_a_delete = ZoomOAuthConnection.objects.create(zoom_oauth_app=self.zoom_oauth_app_a, user_id="user_delete_a", account_id="account_delete_a", state=ZoomOAuthConnectionStates.CONNECTED)
        connection_b_delete = ZoomOAuthConnection.objects.create(zoom_oauth_app=self.zoom_oauth_app_b, user_id="user_delete_b", account_id="account_delete_b", state=ZoomOAuthConnectionStates.CONNECTED)

        # API key A can delete connection from project A
        response = self._make_authenticated_request("DELETE", f"/api/v1/zoom_oauth_connections/{connection_a_delete.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot delete connection from project B
        response = self._make_authenticated_request("DELETE", f"/api/v1/zoom_oauth_connections/{connection_b_delete.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # API key B can delete connection from project B
        response = self._make_authenticated_request("DELETE", f"/api/v1/zoom_oauth_connections/{connection_b_delete.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # Test for cross-project object access protection
    def test_cross_project_object_protection(self):
        """Test that zoom oauth connections from one project cannot be accessed via API key from another project"""
        # Try to access all zoom_oauth_connection_b objects using API key A
        endpoints_to_test = [
            ("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_b.object_id}"),
            ("DELETE", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_b.object_id}"),
        ]

        for method, endpoint in endpoints_to_test:
            with self.subTest(method=method, endpoint=endpoint):
                response = self._make_authenticated_request(method, endpoint, self.api_key_a_plain, "{}")
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(response.json()["error"], "Zoom OAuth Connection not found")

    def test_invalid_api_key_returns_401(self):
        """Test that invalid API keys return 401 Unauthorized"""
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_a.object_id}", "invalid_api_key")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_authorization_header_returns_401(self):
        """Test that missing authorization header returns 401 Unauthorized"""
        response = self.client.get(f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_a.object_id}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_nonexistent_zoom_oauth_connection_returns_404(self):
        """Test that requests for non-existent zoom oauth connections return 404"""
        response = self._make_authenticated_request("GET", "/api/v1/zoom_oauth_connections/zoc_nonexistent12345", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "Zoom OAuth Connection not found")

    def test_zoom_oauth_connection_metadata_is_isolated(self):
        """Test that metadata from different projects is properly isolated"""
        # API key A sees correct metadata for connection A
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["metadata"]["tenant_id"], "tenant_a")

        # API key B sees correct metadata for connection B
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_b.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["metadata"]["tenant_id"], "tenant_b")

    def test_zoom_oauth_connection_state_is_isolated(self):
        """Test that connection state from different projects is properly isolated"""
        # Update connection A to disconnected state
        self.zoom_oauth_connection_a.state = ZoomOAuthConnectionStates.DISCONNECTED
        self.zoom_oauth_connection_a.save()

        # API key A sees disconnected state for connection A
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["state"], "disconnected")

        # API key B still sees connected state for connection B
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_b.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["state"], "connected")

    def test_zoom_oauth_connection_list_pagination(self):
        """Test that zoom oauth connection list pagination respects project boundaries"""
        # Create multiple zoom oauth connections for project A
        for i in range(5):
            ZoomOAuthConnection.objects.create(
                zoom_oauth_app=self.zoom_oauth_app_a,
                user_id=f"user_id_a_{i}",
                account_id=f"account_id_a_{i}",
                state=ZoomOAuthConnectionStates.CONNECTED,
            )

        # Create multiple zoom oauth connections for project B
        for i in range(3):
            ZoomOAuthConnection.objects.create(
                zoom_oauth_app=self.zoom_oauth_app_b,
                user_id=f"user_id_b_{i}",
                account_id=f"account_id_b_{i}",
                state=ZoomOAuthConnectionStates.CONNECTED,
            )

        # API key A should only see connections from project A
        response = self._make_authenticated_request("GET", "/api/v1/zoom_oauth_connections", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json().get("results", response.json())
        if isinstance(results, list):
            # Should see 6 connections from project A (1 original + 5 new)
            self.assertEqual(len(results), 6)
            for conn in results:
                # Verify all connections belong to zoom_oauth_app_a
                connection = ZoomOAuthConnection.objects.get(object_id=conn["id"])
                self.assertEqual(connection.zoom_oauth_app, self.zoom_oauth_app_a)

        # API key B should only see connections from project B
        response = self._make_authenticated_request("GET", "/api/v1/zoom_oauth_connections", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json().get("results", response.json())
        if isinstance(results, list):
            # Should see 4 connections from project B (1 original + 3 new)
            self.assertEqual(len(results), 4)
            for conn in results:
                # Verify all connections belong to zoom_oauth_app_b
                connection = ZoomOAuthConnection.objects.get(object_id=conn["id"])
                self.assertEqual(connection.zoom_oauth_app, self.zoom_oauth_app_b)

    def test_zoom_oauth_connection_credentials_not_exposed(self):
        """Test that encrypted credentials (refresh_token) are not exposed in API responses"""
        # API key A gets connection A
        response = self._make_authenticated_request("GET", f"/api/v1/zoom_oauth_connections/{self.zoom_oauth_connection_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        # Verify that credentials fields are not in the response
        self.assertNotIn("refresh_token", response_data)
        self.assertNotIn("credentials", response_data)
        self.assertNotIn("_encrypted_data", response_data)

        # Verify that expected fields are present
        self.assertIn("id", response_data)
        self.assertIn("state", response_data)
        self.assertIn("metadata", response_data)
        self.assertIn("user_id", response_data)
        self.assertIn("account_id", response_data)

    @patch("bots.zoom_oauth_connections_api_utils._exchange_access_code_for_tokens")
    @patch("bots.zoom_oauth_connections_api_utils._get_user_info")
    @patch("bots.tasks.sync_zoom_oauth_connection_task.enqueue_sync_zoom_oauth_connection_task")
    def test_zoom_oauth_connection_create_with_missing_scopes(self, mock_enqueue_sync, mock_get_user_info, mock_exchange_tokens):
        """Test that zoom oauth connection creation fails with missing required scopes"""
        # Mock the Zoom API calls with insufficient scopes
        mock_exchange_tokens.return_value = {
            "access_token": "access_token_new",
            "refresh_token": "refresh_token_new",
            "scope": "user:read:user",  # Missing required scopes
        }
        mock_get_user_info.return_value = {"id": "new_user_id", "account_id": "new_account_id", "status": "active"}

        connection_data = {
            "zoom_oauth_app_id": self.zoom_oauth_app_a.object_id,
            "authorization_code": "auth_code_123",
            "redirect_uri": "https://example.com/callback",
            "metadata": {"tenant_id": "new_tenant"},
        }

        response = self._make_authenticated_request("POST", "/api/v1/zoom_oauth_connections", self.api_key_a_plain, json.dumps(connection_data))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("missing the following required scopes", response.json()["error"])

    @patch("bots.zoom_oauth_connections_api_utils._exchange_access_code_for_tokens")
    @patch("bots.zoom_oauth_connections_api_utils._get_user_info")
    @patch("bots.tasks.sync_zoom_oauth_connection_task.enqueue_sync_zoom_oauth_connection_task")
    def test_zoom_oauth_connection_create_with_inactive_user(self, mock_enqueue_sync, mock_get_user_info, mock_exchange_tokens):
        """Test that zoom oauth connection creation fails with inactive user"""
        # Mock the Zoom API calls with inactive user
        mock_exchange_tokens.return_value = {
            "access_token": "access_token_new",
            "refresh_token": "refresh_token_new",
            "scope": "user:read:user user:read:zak meeting:read:list_meetings meeting:read:local_recording_token",
        }
        mock_get_user_info.return_value = {"id": "new_user_id", "account_id": "new_account_id", "status": "inactive"}

        connection_data = {
            "zoom_oauth_app_id": self.zoom_oauth_app_a.object_id,
            "authorization_code": "auth_code_123",
            "redirect_uri": "https://example.com/callback",
            "metadata": {"tenant_id": "new_tenant"},
        }

        response = self._make_authenticated_request("POST", "/api/v1/zoom_oauth_connections", self.api_key_a_plain, json.dumps(connection_data))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("user is not active", response.json()["error"])

    def test_multiple_zoom_oauth_connections_per_project(self):
        """Test that a project can have multiple zoom oauth connections"""
        # Create additional zoom oauth connection for project A
        additional_connection_a = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app_a,
            user_id="additional_user_a",
            account_id="additional_account_a",
            state=ZoomOAuthConnectionStates.CONNECTED,
            metadata={"tenant_id": "additional_tenant_a"},
        )

        # API key A should see both connections
        response = self._make_authenticated_request("GET", "/api/v1/zoom_oauth_connections", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json().get("results", response.json())
        if isinstance(results, list):
            connection_ids = [conn["id"] for conn in results]
            self.assertIn(self.zoom_oauth_connection_a.object_id, connection_ids)
            self.assertIn(additional_connection_a.object_id, connection_ids)
            # Should have at least 2 connections
            self.assertGreaterEqual(len(results), 2)

        # API key B should not see connections from project A
        response = self._make_authenticated_request("GET", "/api/v1/zoom_oauth_connections", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json().get("results", response.json())
        if isinstance(results, list):
            connection_ids = [conn["id"] for conn in results]
            self.assertNotIn(self.zoom_oauth_connection_a.object_id, connection_ids)
            self.assertNotIn(additional_connection_a.object_id, connection_ids)
