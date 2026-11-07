from unittest.mock import patch

from django.db import models
from django.test import TestCase

from accounts.models import Organization
from bots.models import (
    Project,
    ZoomOAuthApp,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)
from bots.zoom_oauth_apps_api_utils import create_or_update_zoom_oauth_app


class TestCreateOrUpdateZoomOAuthApp(TestCase):
    """Test the create_or_update_zoom_oauth_app function."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_create_zoom_oauth_app_success(self, mock_is_valid):
        """Test successful creation of a new zoom oauth app with valid credentials."""
        mock_is_valid.return_value = True

        zoom_oauth_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="test_client_secret",
            webhook_secret="test_webhook_secret",
        )

        # Verify successful creation
        self.assertIsNotNone(zoom_oauth_app)
        self.assertIsNone(error)

        # Verify app properties
        self.assertEqual(zoom_oauth_app.project, self.project)
        self.assertEqual(zoom_oauth_app.client_id, "test_client_id")
        self.assertIsNotNone(zoom_oauth_app.object_id)
        self.assertTrue(zoom_oauth_app.object_id.startswith("zoa_"))

        # Verify credentials are encrypted and stored
        credentials = zoom_oauth_app.get_credentials()
        self.assertIsNotNone(credentials)
        self.assertEqual(credentials["client_secret"], "test_client_secret")
        self.assertEqual(credentials["webhook_secret"], "test_webhook_secret")

        # Verify validation was called
        mock_is_valid.assert_called_once_with("test_client_id", "test_client_secret")

    def test_create_zoom_oauth_app_missing_client_id(self):
        """Test creation fails when client_id is missing."""
        zoom_oauth_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="",
            client_secret="test_client_secret",
            webhook_secret="test_webhook_secret",
        )

        # Verify creation failed
        self.assertIsNone(zoom_oauth_app)
        self.assertIsNotNone(error)
        self.assertIn("client_id and client_secret are required", error)

    def test_create_zoom_oauth_app_missing_client_secret(self):
        """Test creation fails when client_secret is missing."""
        zoom_oauth_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="",
            webhook_secret="test_webhook_secret",
        )

        # Verify creation failed
        self.assertIsNone(zoom_oauth_app)
        self.assertIsNotNone(error)
        self.assertIn("client_id and client_secret are required", error)

    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_create_zoom_oauth_app_invalid_credentials(self, mock_is_valid):
        """Test creation fails when credentials are invalid."""
        mock_is_valid.return_value = False

        zoom_oauth_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="invalid_client_id",
            client_secret="invalid_client_secret",
            webhook_secret="test_webhook_secret",
        )

        # Verify creation failed
        self.assertIsNone(zoom_oauth_app)
        self.assertIsNotNone(error)
        self.assertIn("Invalid client id or client secret", error)

        # Verify validation was called
        mock_is_valid.assert_called_once_with("invalid_client_id", "invalid_client_secret")

    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_create_zoom_oauth_app_with_whitespace_secrets(self, mock_is_valid):
        """Test creation with secrets that have leading/trailing whitespace."""
        mock_is_valid.return_value = True

        zoom_oauth_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="  test_client_secret  ",
            webhook_secret="  test_webhook_secret  ",
        )

        # Verify successful creation
        self.assertIsNotNone(zoom_oauth_app)
        self.assertIsNone(error)

        # Verify secrets were stripped
        credentials = zoom_oauth_app.get_credentials()
        self.assertEqual(credentials["client_secret"], "test_client_secret")
        self.assertEqual(credentials["webhook_secret"], "test_webhook_secret")

    @patch("bots.zoom_oauth_apps_api_utils.validate_zoom_oauth_connections")
    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_update_zoom_oauth_app_client_secret_success(self, mock_is_valid, mock_validate_task):
        """Test successful update of client secret for existing zoom oauth app."""
        mock_is_valid.return_value = True

        # Create initial app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "old_client_secret", "webhook_secret": "old_webhook_secret"})
        app_id = zoom_oauth_app.id

        # Update with new client secret
        updated_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",  # This should be ignored for updates
            client_secret="new_client_secret",
            webhook_secret="",  # Empty should preserve old value
        )

        # Verify successful update
        self.assertIsNotNone(updated_app)
        self.assertIsNone(error)
        self.assertEqual(updated_app.id, app_id)

        # Verify credentials were updated
        credentials = updated_app.get_credentials()
        self.assertEqual(credentials["client_secret"], "new_client_secret")
        self.assertEqual(credentials["webhook_secret"], "old_webhook_secret")  # Preserved

        # Verify validation was called
        mock_is_valid.assert_called_once_with("test_client_id", "new_client_secret")

        # Verify validate_zoom_oauth_connections task was triggered
        mock_validate_task.delay.assert_called_once_with(zoom_oauth_app.id)

    @patch("bots.zoom_oauth_apps_api_utils.validate_zoom_oauth_connections")
    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_update_zoom_oauth_app_client_secret_same_value(self, mock_is_valid, mock_validate_task):
        """Test updating client secret with same value does not trigger validation."""
        mock_is_valid.return_value = True

        # Create initial app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "same_secret", "webhook_secret": "old_webhook_secret"})

        # Update with same client secret
        updated_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="same_secret",
            webhook_secret="",
        )

        # Verify successful update
        self.assertIsNotNone(updated_app)
        self.assertIsNone(error)

        # Verify validation was called
        mock_is_valid.assert_called_once_with("test_client_id", "same_secret")

        # Verify validate_zoom_oauth_connections task was NOT triggered
        mock_validate_task.delay.assert_not_called()

    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_update_zoom_oauth_app_invalid_client_secret(self, mock_is_valid):
        """Test update fails when new client secret is invalid."""
        # Create initial app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "old_client_secret", "webhook_secret": "old_webhook_secret"})

        mock_is_valid.return_value = False

        # Try to update with invalid client secret
        updated_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="invalid_secret",
            webhook_secret="",
        )

        # Verify update failed
        self.assertIsNone(updated_app)
        self.assertIsNotNone(error)
        self.assertIn("Invalid client secret", error)

        # Verify validation was called
        mock_is_valid.assert_called_once_with("test_client_id", "invalid_secret")

    def test_update_zoom_oauth_app_webhook_secret_only(self):
        """Test updating only the webhook secret without changing client secret."""
        # Create initial app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "old_client_secret", "webhook_secret": "old_webhook_secret"})
        app_id = zoom_oauth_app.id

        # Update only webhook secret
        updated_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="",  # Empty should preserve old value
            webhook_secret="new_webhook_secret",
        )

        # Verify successful update
        self.assertIsNotNone(updated_app)
        self.assertIsNone(error)
        self.assertEqual(updated_app.id, app_id)

        # Verify credentials
        credentials = updated_app.get_credentials()
        self.assertEqual(credentials["client_secret"], "old_client_secret")  # Preserved
        self.assertEqual(credentials["webhook_secret"], "new_webhook_secret")  # Updated

    @patch("bots.zoom_oauth_apps_api_utils.validate_zoom_oauth_connections")
    @patch("bots.zoom_oauth_apps_api_utils.client_id_and_secret_is_valid")
    def test_update_zoom_oauth_app_both_secrets(self, mock_is_valid, mock_validate_task):
        """Test updating both client_secret and webhook_secret."""
        mock_is_valid.return_value = True

        # Create initial app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "old_client_secret", "webhook_secret": "old_webhook_secret"})
        app_id = zoom_oauth_app.id

        # Update both secrets
        updated_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="new_client_secret",
            webhook_secret="new_webhook_secret",
        )

        # Verify successful update
        self.assertIsNotNone(updated_app)
        self.assertIsNone(error)
        self.assertEqual(updated_app.id, app_id)

        # Verify both credentials were updated
        credentials = updated_app.get_credentials()
        self.assertEqual(credentials["client_secret"], "new_client_secret")
        self.assertEqual(credentials["webhook_secret"], "new_webhook_secret")

        # Verify validation was called
        mock_is_valid.assert_called_once_with("test_client_id", "new_client_secret")

        # Verify validate_zoom_oauth_connections task was triggered
        mock_validate_task.delay.assert_called_once_with(zoom_oauth_app.id)

    def test_update_zoom_oauth_app_empty_secrets_preserves_existing(self):
        """Test that providing empty strings for both secrets preserves existing values."""
        # Create initial app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "existing_client_secret", "webhook_secret": "existing_webhook_secret"})
        app_id = zoom_oauth_app.id

        # Update with empty secrets
        updated_app, error = create_or_update_zoom_oauth_app(
            project=self.project,
            client_id="test_client_id",
            client_secret="",
            webhook_secret="",
        )

        # Verify successful update
        self.assertIsNotNone(updated_app)
        self.assertIsNone(error)
        self.assertEqual(updated_app.id, app_id)

        # Verify credentials were preserved
        credentials = updated_app.get_credentials()
        self.assertEqual(credentials["client_secret"], "existing_client_secret")
        self.assertEqual(credentials["webhook_secret"], "existing_webhook_secret")

    def test_credential_properties_when_encrypted_data_missing(self):
        """Missing encrypted credentials should return None, not AttributeError."""
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id_no_creds")
        self.assertIsNone(zoom_oauth_app.client_secret)
        self.assertIsNone(zoom_oauth_app.webhook_secret)


class TestZoomOAuthAppDeletion(TestCase):
    """Test deletion constraints for ZoomOAuthApp."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_delete_zoom_oauth_app_without_connections(self):
        """Test that a ZoomOAuthApp can be deleted when it has no associated connections."""
        # Create a zoom oauth app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "test_client_secret", "webhook_secret": "test_webhook_secret"})
        app_id = zoom_oauth_app.id

        # Verify it exists
        self.assertEqual(ZoomOAuthApp.objects.filter(id=app_id).count(), 1)

        # Delete the app
        zoom_oauth_app.delete()

        # Verify it was deleted
        self.assertEqual(ZoomOAuthApp.objects.filter(id=app_id).count(), 0)

    def test_cannot_delete_zoom_oauth_app_with_connections(self):
        """Test that a ZoomOAuthApp cannot be deleted when it has associated connections (PROTECT constraint)."""
        # Create a zoom oauth app
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        zoom_oauth_app.set_credentials({"client_secret": "test_client_secret", "webhook_secret": "test_webhook_secret"})

        # Create a zoom oauth connection associated with this app
        zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="test_user_id",
            account_id="test_account_id",
            state=ZoomOAuthConnectionStates.CONNECTED,
        )
        zoom_oauth_connection.set_credentials({"refresh_token": "test_refresh_token"})

        # Attempt to delete the app - should raise ProtectedError
        with self.assertRaises(models.ProtectedError):
            zoom_oauth_app.delete()

        # Verify the app still exists
        zoom_oauth_app.refresh_from_db()
        self.assertIsNotNone(zoom_oauth_app)

    def test_multiple_apps_deletion_independence(self):
        """Test that deleting one ZoomOAuthApp doesn't affect another app's connections."""
        # Create two zoom oauth apps
        zoom_oauth_app1 = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id_1")
        zoom_oauth_app1.set_credentials({"client_secret": "test_client_secret_1", "webhook_secret": "test_webhook_secret_1"})

        zoom_oauth_app2 = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id_2")
        zoom_oauth_app2.set_credentials({"client_secret": "test_client_secret_2", "webhook_secret": "test_webhook_secret_2"})

        # Create connections for both apps
        connection1 = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app1,
            user_id="test_user_id_1",
            account_id="test_account_id_1",
            state=ZoomOAuthConnectionStates.CONNECTED,
        )
        connection1.set_credentials({"refresh_token": "test_refresh_token_1"})

        connection2 = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app2,
            user_id="test_user_id_2",
            account_id="test_account_id_2",
            state=ZoomOAuthConnectionStates.CONNECTED,
        )
        connection2.set_credentials({"refresh_token": "test_refresh_token_2"})

        # Delete connection1 and app1
        connection1.delete()
        zoom_oauth_app1.delete()

        # Verify app2 and connection2 still exist
        zoom_oauth_app2.refresh_from_db()
        connection2.refresh_from_db()
        self.assertIsNotNone(zoom_oauth_app2)
        self.assertIsNotNone(connection2)

        # Verify app2 still cannot be deleted due to connection2
        with self.assertRaises(models.ProtectedError):
            zoom_oauth_app2.delete()
