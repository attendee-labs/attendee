from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.models import (
    Project,
    ZoomOAuthApp,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)
from bots.tasks.refresh_zoom_oauth_connection_task import (
    refresh_zoom_oauth_connection,
)
from bots.zoom_oauth_connections_utils import ZoomAPIAuthenticationError


@contextmanager
def _noop_lock(*args, **kwargs):
    yield


class TestRefreshZoomOAuthConnection(TestCase):
    """Test the refresh_zoom_oauth_connection Celery task."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        self.zoom_oauth_app.set_credentials({"client_secret": "test_secret"})
        self.zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            user_id="test_user_id",
            account_id="test_account_id",
        )
        self.zoom_oauth_connection.set_credentials({"refresh_token": "test_refresh_token"})
        self.lock_patcher = patch(
            "bots.tasks.refresh_zoom_oauth_connection_task.zoom_oauth_connection_token_lock",
            side_effect=_noop_lock,
        )
        self.lock_patcher.start()

    def tearDown(self):
        self.lock_patcher.stop()

    @patch("bots.tasks.refresh_zoom_oauth_connection_task._get_access_token")
    def test_refresh_zoom_oauth_connection_success(self, mock_get_access_token):
        """Test successful refresh of zoom oauth connection token."""
        mock_get_access_token.return_value = "mock_access_token"

        refresh_zoom_oauth_connection(self.zoom_oauth_connection.id)

        mock_get_access_token.assert_called_once_with(self.zoom_oauth_connection)

        self.zoom_oauth_connection.refresh_from_db()
        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.CONNECTED)
        self.assertIsNotNone(self.zoom_oauth_connection.last_attempted_token_refresh_at)
        self.assertIsNotNone(self.zoom_oauth_connection.last_successful_token_refresh_at)
        self.assertIsNone(self.zoom_oauth_connection.connection_failure_data)

    @patch("bots.tasks.refresh_zoom_oauth_connection_task._get_access_token")
    @patch("bots.tasks.refresh_zoom_oauth_connection_task.zoom_oauth_connection_token_lock")
    def test_refresh_acquires_connection_scoped_lock(self, mock_lock, mock_get_access_token):
        """Refresh must take the connection token lock before rotating credentials."""
        mock_lock.side_effect = _noop_lock
        mock_get_access_token.return_value = "mock_access_token"

        refresh_zoom_oauth_connection(self.zoom_oauth_connection.id)

        mock_lock.assert_called_with(self.zoom_oauth_connection.id)


    @patch("bots.zoom_oauth_connections_utils.trigger_webhook")
    @patch("bots.tasks.refresh_zoom_oauth_connection_task._get_access_token")
    def test_refresh_zoom_oauth_connection_authentication_error(self, mock_get_access_token, mock_trigger_webhook):
        """Test refresh handles authentication errors properly."""
        auth_error = ZoomAPIAuthenticationError("Invalid credentials")
        mock_get_access_token.side_effect = auth_error

        refresh_zoom_oauth_connection(self.zoom_oauth_connection.id)

        self.zoom_oauth_connection.refresh_from_db()
        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.DISCONNECTED)
        self.assertIsNotNone(self.zoom_oauth_connection.connection_failure_data)
        self.assertIn("Invalid credentials", self.zoom_oauth_connection.connection_failure_data["error"])
        self.assertIn("timestamp", self.zoom_oauth_connection.connection_failure_data)

    @patch("bots.tasks.refresh_zoom_oauth_connection_task._get_access_token")
    def test_refresh_zoom_oauth_connection_general_exception(self, mock_get_access_token):
        """Test refresh handles general exceptions properly."""
        mock_get_access_token.side_effect = Exception("Network error")

        with self.assertRaises(Exception) as cm:
            refresh_zoom_oauth_connection(self.zoom_oauth_connection.id)

        self.assertIn("Network error", str(cm.exception))

        self.zoom_oauth_connection.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_connection.last_attempted_token_refresh_at)

    @patch("bots.tasks.refresh_zoom_oauth_connection_task._get_access_token")
    def test_refresh_zoom_oauth_connection_clears_failure_data(self, mock_get_access_token):
        """Test that successful refresh clears previous connection failure data."""
        mock_get_access_token.return_value = "mock_access_token"

        # Set initial failure state
        self.zoom_oauth_connection.state = ZoomOAuthConnectionStates.DISCONNECTED
        self.zoom_oauth_connection.connection_failure_data = {
            "error": "Previous error",
            "timestamp": timezone.now().isoformat(),
        }
        self.zoom_oauth_connection.save()

        refresh_zoom_oauth_connection(self.zoom_oauth_connection.id)

        self.zoom_oauth_connection.refresh_from_db()

        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.CONNECTED)
        self.assertIsNone(self.zoom_oauth_connection.connection_failure_data)
