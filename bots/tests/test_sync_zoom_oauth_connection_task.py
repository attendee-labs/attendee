from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.models import (
    Project,
    ZoomMeetingToZoomOAuthConnectionMapping,
    ZoomOAuthApp,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)
from bots.tasks.sync_zoom_oauth_connection_task import (
    sync_zoom_oauth_connection,
)
from bots.zoom_oauth_connections_utils import ZoomAPIAuthenticationError


@contextmanager
def _noop_lock(*args, **kwargs):
    yield


class TestSyncZoomOAuthConnection(TestCase):
    """Test the sync_zoom_oauth_connection Celery task."""

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
            "bots.tasks.sync_zoom_oauth_connection_task.zoom_oauth_connection_token_lock",
            side_effect=_noop_lock,
        )
        self.lock_patcher.start()

    def tearDown(self):
        self.lock_patcher.stop()

    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_access_token")
    @patch("bots.tasks.sync_zoom_oauth_connection_task.zoom_oauth_connection_token_lock")
    def test_sync_acquires_connection_scoped_lock(self, mock_lock, mock_get_access_token):
        """Sync must share the same connection token lock as refresh."""
        mock_lock.side_effect = _noop_lock
        mock_get_access_token.return_value = "mock_access_token"

        with patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_meetings", return_value=[]):
            with patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_personal_meeting_id", return_value="pmi"):
                with patch("bots.tasks.sync_zoom_oauth_connection_task._upsert_zoom_meeting_to_zoom_oauth_connection_mapping"):
                    sync_zoom_oauth_connection(self.zoom_oauth_connection.id)

        mock_lock.assert_called_with(self.zoom_oauth_connection.id)

    @patch("bots.tasks.sync_zoom_oauth_connection_task._upsert_zoom_meeting_to_zoom_oauth_connection_mapping")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_personal_meeting_id")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_meetings")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_access_token")
    def test_sync_zoom_oauth_connection_success(
        self,
        mock_get_access_token,
        mock_get_zoom_meetings,
        mock_get_personal_meeting_id,
        mock_upsert_mapping,
    ):
        """Test successful sync of zoom oauth connection."""
        mock_get_access_token.return_value = "mock_access_token"
        mock_get_zoom_meetings.return_value = [
            {"id": "111111111", "topic": "Meeting 1"},
            {"id": "222222222", "topic": "Meeting 2"},
        ]
        mock_get_personal_meeting_id.return_value = "333333333"

        sync_zoom_oauth_connection(self.zoom_oauth_connection.id)

        # Verify all functions were called correctly
        mock_get_access_token.assert_called_once_with(self.zoom_oauth_connection)
        mock_get_zoom_meetings.assert_called_once_with("mock_access_token")
        mock_get_personal_meeting_id.assert_called_once_with("mock_access_token")
        mock_upsert_mapping.assert_called_once_with(
            ["111111111", "222222222", "333333333"],
            self.zoom_oauth_connection,
        )

        # Verify connection state is updated
        self.zoom_oauth_connection.refresh_from_db()
        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.CONNECTED)
        self.assertIsNotNone(self.zoom_oauth_connection.last_attempted_sync_at)
        self.assertIsNotNone(self.zoom_oauth_connection.last_successful_sync_at)
        self.assertIsNotNone(self.zoom_oauth_connection.last_successful_sync_started_at)
        self.assertIsNone(self.zoom_oauth_connection.connection_failure_data)

    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_access_token")
    def test_sync_zoom_oauth_connection_no_meetings(self, mock_get_access_token):
        """Test sync with no meetings returned from Zoom."""
        mock_get_access_token.return_value = "mock_access_token"

        with patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_meetings") as mock_get_meetings:
            with patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_personal_meeting_id") as mock_get_pmi:
                with patch("bots.tasks.sync_zoom_oauth_connection_task._upsert_zoom_meeting_to_zoom_oauth_connection_mapping") as mock_upsert:
                    mock_get_meetings.return_value = []
                    mock_get_pmi.return_value = "333333333"

                    sync_zoom_oauth_connection(self.zoom_oauth_connection.id)

                    # Should still call upsert with just the PMI
                    mock_upsert.assert_called_once_with(["333333333"], self.zoom_oauth_connection)

        self.zoom_oauth_connection.refresh_from_db()
        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.CONNECTED)

    @patch("bots.tasks.sync_zoom_oauth_connection_task._handle_zoom_api_authentication_error")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_access_token")
    def test_sync_zoom_oauth_connection_authentication_error(self, mock_get_access_token, mock_handle_auth_error):
        """Test sync handles authentication errors properly."""
        auth_error = ZoomAPIAuthenticationError("Invalid credentials")
        mock_get_access_token.side_effect = auth_error

        sync_zoom_oauth_connection(self.zoom_oauth_connection.id)

        # Verify error handler was called
        mock_handle_auth_error.assert_called_once_with(self.zoom_oauth_connection, auth_error)

    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_access_token")
    def test_sync_zoom_oauth_connection_general_exception(self, mock_get_access_token):
        """Test sync handles general exceptions properly."""
        mock_get_access_token.side_effect = Exception("Network error")

        with self.assertRaises(Exception) as cm:
            sync_zoom_oauth_connection(self.zoom_oauth_connection.id)

        self.assertIn("Network error", str(cm.exception))

        # Verify last_attempted_sync_at is updated even on error
        self.zoom_oauth_connection.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_connection.last_attempted_sync_at)

    @patch("bots.tasks.sync_zoom_oauth_connection_task._upsert_zoom_meeting_to_zoom_oauth_connection_mapping")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_personal_meeting_id")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_meetings")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_access_token")
    def test_sync_zoom_oauth_connection_updates_timestamps(
        self,
        mock_get_access_token,
        mock_get_zoom_meetings,
        mock_get_personal_meeting_id,
        mock_upsert_mapping,
    ):
        """Test that sync properly updates all timestamp fields."""
        mock_get_access_token.return_value = "mock_access_token"
        mock_get_zoom_meetings.return_value = [{"id": "111111111"}]
        mock_get_personal_meeting_id.return_value = "222222222"

        # Set initial timestamps
        past_time = timezone.now() - timedelta(days=1)
        self.zoom_oauth_connection.last_attempted_sync_at = past_time
        self.zoom_oauth_connection.last_successful_sync_at = past_time
        self.zoom_oauth_connection.last_successful_sync_started_at = past_time
        self.zoom_oauth_connection.save()

        sync_zoom_oauth_connection(self.zoom_oauth_connection.id)

        self.zoom_oauth_connection.refresh_from_db()

        # Verify all timestamps are updated to recent times
        self.assertGreater(self.zoom_oauth_connection.last_attempted_sync_at, past_time)
        self.assertGreater(self.zoom_oauth_connection.last_successful_sync_at, past_time)
        self.assertGreater(self.zoom_oauth_connection.last_successful_sync_started_at, past_time)
        self.assertEqual(
            self.zoom_oauth_connection.last_attempted_sync_at,
            self.zoom_oauth_connection.last_successful_sync_at,
        )

    @patch("bots.tasks.sync_zoom_oauth_connection_task._upsert_zoom_meeting_to_zoom_oauth_connection_mapping")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_personal_meeting_id")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_zoom_meetings")
    @patch("bots.tasks.sync_zoom_oauth_connection_task._get_access_token")
    def test_sync_zoom_oauth_connection_clears_failure_data(
        self,
        mock_get_access_token,
        mock_get_zoom_meetings,
        mock_get_personal_meeting_id,
        mock_upsert_mapping,
    ):
        """Test that successful sync clears previous connection failure data."""
        mock_get_access_token.return_value = "mock_access_token"
        mock_get_zoom_meetings.return_value = [{"id": "111111111"}]
        mock_get_personal_meeting_id.return_value = "222222222"

        # Set initial failure state
        self.zoom_oauth_connection.state = ZoomOAuthConnectionStates.DISCONNECTED
        self.zoom_oauth_connection.connection_failure_data = {
            "error": "Previous error",
            "timestamp": timezone.now().isoformat(),
        }
        self.zoom_oauth_connection.save()

        sync_zoom_oauth_connection(self.zoom_oauth_connection.id)

        self.zoom_oauth_connection.refresh_from_db()

        # Verify state is restored and failure data is cleared
        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.CONNECTED)
        self.assertIsNone(self.zoom_oauth_connection.connection_failure_data)


class TestGetAccessToken(TestCase):
    """Test the _get_access_token function."""

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
            "bots.redis_utils.zoom_oauth_connection_token_lock",
            side_effect=_noop_lock,
        )
        self.lock_patcher.start()

    def tearDown(self):
        self.lock_patcher.stop()

    @patch("requests.post")
    def test_get_access_token_success(self, mock_post):
        """Test successful access token retrieval."""
        from bots.zoom_oauth_connections_utils import _get_access_token

        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "new_access_token"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = _get_access_token(self.zoom_oauth_connection)

        self.assertEqual(result, "new_access_token")
        mock_post.assert_called_once()

    @patch("requests.post")
    @patch("bots.redis_utils.zoom_oauth_connection_token_lock")
    def test_get_access_token_takes_lock_and_reloads_credentials(self, mock_lock, mock_post):
        """Token refresh must lock the connection and reload credentials under the lock."""
        from bots.zoom_oauth_connections_utils import _get_access_token

        mock_lock.side_effect = _noop_lock
        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "rotated_refresh_token",
        }
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = _get_access_token(self.zoom_oauth_connection)

        mock_lock.assert_called_with(self.zoom_oauth_connection.id)
        self.assertEqual(result, "new_access_token")
        self.zoom_oauth_connection.refresh_from_db()
        self.assertEqual(self.zoom_oauth_connection.get_credentials()["refresh_token"], "rotated_refresh_token")

    @patch("requests.post")
    def test_get_access_token_with_refresh_token_rotation(self, mock_post):
        """Test access token retrieval with Zoom's token rotation."""
        from bots.zoom_oauth_connections_utils import _get_access_token

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
        }
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        original_credentials = self.zoom_oauth_connection.get_credentials()

        result = _get_access_token(self.zoom_oauth_connection)

        self.assertEqual(result, "new_access_token")

        self.zoom_oauth_connection.refresh_from_db()

        # Verify new refresh token is saved
        updated_credentials = self.zoom_oauth_connection.get_credentials()
        self.assertEqual(updated_credentials["refresh_token"], "new_refresh_token")
        self.assertNotEqual(updated_credentials["refresh_token"], original_credentials["refresh_token"])

    @patch("requests.post")
    def test_get_access_token_invalid_grant(self, mock_post):
        """Test access token retrieval with invalid grant error."""
        from bots.zoom_oauth_connections_utils import _get_access_token

        mock_response = Mock()
        mock_response.json.return_value = {"error": "invalid_grant"}

        # Create a proper RequestException with a response attribute
        exception = requests.RequestException()
        exception.response = mock_response
        mock_response.raise_for_status.side_effect = exception
        mock_post.return_value = mock_response

        with self.assertRaises(ZoomAPIAuthenticationError):
            _get_access_token(self.zoom_oauth_connection)

    @patch("requests.post")
    def test_get_access_token_invalid_client(self, mock_post):
        """Test access token retrieval with invalid client error."""
        from bots.zoom_oauth_connections_utils import _get_access_token

        mock_response = Mock()
        mock_response.json.return_value = {"error": "invalid_client"}

        exception = requests.RequestException()
        exception.response = mock_response
        mock_response.raise_for_status.side_effect = exception
        mock_post.return_value = mock_response

        with self.assertRaises(ZoomAPIAuthenticationError):
            _get_access_token(self.zoom_oauth_connection)

    def test_get_access_token_no_credentials(self):
        """Test access token retrieval when no credentials are stored."""
        from bots.zoom_oauth_connections_utils import _get_access_token

        # Create connection without credentials
        connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            user_id="test_user_2",
            account_id="test_account_2",
        )

        with self.assertRaises(ZoomAPIAuthenticationError) as cm:
            _get_access_token(connection)

        self.assertIn("No credentials found", str(cm.exception))

    def test_get_access_token_missing_refresh_token(self):
        """Test access token retrieval when refresh token is missing."""
        from bots.zoom_oauth_connections_utils import _get_access_token

        self.zoom_oauth_connection.set_credentials({"access_token": "test_access_token"})

        with self.assertRaises(ZoomAPIAuthenticationError) as cm:
            _get_access_token(self.zoom_oauth_connection)

        self.assertIn("Missing refresh_token", str(cm.exception))

    @patch("requests.post")
    def test_get_access_token_no_access_token_in_response(self, mock_post):
        """Test when Zoom API returns response without access_token."""
        from bots.zoom_oauth_connections_utils import ZoomAPIError, _get_access_token

        mock_response = Mock()
        mock_response.json.return_value = {"refresh_token": "new_refresh"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        with self.assertRaises(ZoomAPIError) as cm:
            _get_access_token(self.zoom_oauth_connection)

        self.assertIn("No access_token in refresh response", str(cm.exception))


class TestUpsertZoomMeetingMapping(TestCase):
    """Test the _upsert_zoom_meeting_to_zoom_oauth_connection_mapping function."""

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

    def test_upsert_creates_new_mappings(self):
        """Test creating new meeting mappings."""
        from bots.zoom_oauth_connections_utils import _upsert_zoom_meeting_to_zoom_oauth_connection_mapping

        meeting_ids = ["111111111", "222222222", "333333333"]

        _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(meeting_ids, self.zoom_oauth_connection)

        # Verify mappings were created
        mappings = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(zoom_oauth_app=self.zoom_oauth_app)
        self.assertEqual(mappings.count(), 3)

        meeting_id_list = [m.meeting_id for m in mappings]
        self.assertIn("111111111", meeting_id_list)
        self.assertIn("222222222", meeting_id_list)
        self.assertIn("333333333", meeting_id_list)

    def test_upsert_updates_existing_mappings(self):
        """Test updating existing meeting mappings to new connection."""
        from bots.zoom_oauth_connections_utils import _upsert_zoom_meeting_to_zoom_oauth_connection_mapping

        # Create another connection
        other_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            user_id="other_user_id",
            account_id="other_account_id",
        )

        # Create existing mapping pointing to other connection
        existing_mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            zoom_oauth_connection=other_connection,
            meeting_id="111111111",
        )

        # Upsert should update the mapping to point to our connection
        _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(["111111111"], self.zoom_oauth_connection)

        existing_mapping.refresh_from_db()
        self.assertEqual(existing_mapping.zoom_oauth_connection, self.zoom_oauth_connection)

    def test_upsert_maintains_existing_correct_mappings(self):
        """Test that existing correct mappings are maintained."""
        from bots.zoom_oauth_connections_utils import _upsert_zoom_meeting_to_zoom_oauth_connection_mapping

        # Create existing mapping
        existing_mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            zoom_oauth_connection=self.zoom_oauth_connection,
            meeting_id="111111111",
        )
        original_created_at = existing_mapping.created_at

        # Upsert same meeting ID
        _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(["111111111"], self.zoom_oauth_connection)

        existing_mapping.refresh_from_db()
        # Should still point to same connection
        self.assertEqual(existing_mapping.zoom_oauth_connection, self.zoom_oauth_connection)
        # Created_at should not change
        self.assertEqual(existing_mapping.created_at, original_created_at)

    def test_upsert_handles_none_meeting_ids(self):
        """Test that None meeting IDs are skipped."""
        from bots.zoom_oauth_connections_utils import _upsert_zoom_meeting_to_zoom_oauth_connection_mapping

        meeting_ids = ["111111111", None, "222222222"]

        _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(meeting_ids, self.zoom_oauth_connection)

        # Verify only non-None mappings were created
        mappings = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(zoom_oauth_app=self.zoom_oauth_app)
        self.assertEqual(mappings.count(), 2)

    def test_upsert_empty_list(self):
        """Test upserting with empty meeting ID list."""
        from bots.zoom_oauth_connections_utils import _upsert_zoom_meeting_to_zoom_oauth_connection_mapping

        _upsert_zoom_meeting_to_zoom_oauth_connection_mapping([], self.zoom_oauth_connection)

        # Verify no mappings were created
        mappings = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(zoom_oauth_app=self.zoom_oauth_app)
        self.assertEqual(mappings.count(), 0)
