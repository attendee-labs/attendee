import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Organization
from bots.models import (
    Project,
    ZoomMeetingToZoomOAuthConnectionMapping,
    ZoomOAuthApp,
    ZoomOAuthConnection,
)


class TestZoomOAuthWebhooks(TestCase):
    """Test the Zoom OAuth app webhook events."""

    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        self.zoom_oauth_app.set_credentials({"client_secret": "test_secret", "webhook_secret": "test_webhook_secret"})
        self.zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            user_id="test_user_123",
            account_id="test_account_id",
        )
        self.url = reverse("external_webhooks:external-webhook-zoom-oauth-app", kwargs={"object_id": self.zoom_oauth_app.object_id})

    def _generate_zoom_signature(self, body: str, timestamp: str, secret: str) -> str:
        """Generate a valid Zoom webhook signature."""
        hmac_hash = hmac.new(secret.encode("utf-8"), f"v0:{timestamp}:{body}".encode("utf-8"), hashlib.sha256).hexdigest()
        return f"v0={hmac_hash}"

    def test_meeting_created_event_success(self):
        """Test successful handling of meeting.created event."""
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "123456789", "host_id": "test_user_123"},
                "operator_id": "test_user_123",
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 200)

        # Verify mapping was created
        mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="123456789", zoom_oauth_connection=self.zoom_oauth_connection).first()
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.zoom_oauth_app, self.zoom_oauth_app)

        # Verify last_verified_webhook_received_at was updated
        self.zoom_oauth_app.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_app.last_verified_webhook_received_at)
        self.assertIsNone(self.zoom_oauth_app.last_unverified_webhook_received_at)

    def test_meeting_created_event_operator_differs_from_host(self):
        """Test meeting.created event when operator_id differs from host_id (should still work)."""
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "987654321", "host_id": "host_user_456"},
                "operator_id": "test_user_123",
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 200)

        # Verify mapping was created with operator's connection
        mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="987654321").first()
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.zoom_oauth_connection, self.zoom_oauth_connection)

    def test_meeting_created_event_no_connection_found(self):
        """Test meeting.created event when no ZoomOAuthConnection exists for the operator."""
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "123456789", "host_id": "unknown_user"},
                "operator_id": "unknown_user",
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        # Should return 200 but not create mapping
        self.assertEqual(response.status_code, 200)

        # Verify no mapping was created
        mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="123456789").first()
        self.assertIsNone(mapping)

    def test_user_updated_event_pmi_changed_success(self):
        """Test successful handling of user.updated event when PMI changes."""
        event_data = {
            "event": "user.updated",
            "payload": {
                "object": {"id": "test_user_123", "pmi": "5551234567"},
                "old_object": {"id": "test_user_123", "pmi": "5559876543"},
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 200)

        # Verify mapping was created for the new PMI
        mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="5551234567", zoom_oauth_connection=self.zoom_oauth_connection).first()
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.zoom_oauth_app, self.zoom_oauth_app)

        # Verify last_verified_webhook_received_at was updated
        self.zoom_oauth_app.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_app.last_verified_webhook_received_at)
        self.assertIsNone(self.zoom_oauth_app.last_unverified_webhook_received_at)

    def test_user_updated_event_pmi_unchanged(self):
        """Test user.updated event when PMI has not changed (should do nothing)."""
        event_data = {
            "event": "user.updated",
            "payload": {
                "object": {"id": "test_user_123", "pmi": "5551234567"},
                "old_object": {"id": "test_user_123", "pmi": "5551234567"},
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        # Should return 200 but not create mapping
        self.assertEqual(response.status_code, 200)

        # Verify no mapping was created
        mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="5551234567").first()
        self.assertIsNone(mapping)

    def test_user_updated_event_new_pmi_is_none(self):
        """Test user.updated event when new PMI is None (should do nothing)."""
        event_data = {
            "event": "user.updated",
            "payload": {
                "object": {"id": "test_user_123", "pmi": None},
                "old_object": {"id": "test_user_123", "pmi": "5551234567"},
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        # Should return 200 but not create mapping
        self.assertEqual(response.status_code, 200)

        # Verify no mapping was created
        mappings = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(zoom_oauth_connection=self.zoom_oauth_connection)
        self.assertEqual(mappings.count(), 0)

    def test_user_updated_event_user_id_is_none(self):
        """Test user.updated event when new user ID is None (should do nothing)."""
        event_data = {
            "event": "user.updated",
            "payload": {
                "object": {"id": None, "pmi": "5551234567"},
                "old_object": {"id": "test_user_123", "pmi": "5559876543"},
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        # Should return 200 but not create mapping
        self.assertEqual(response.status_code, 200)

        # Verify no mapping was created
        mappings = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(zoom_oauth_connection=self.zoom_oauth_connection)
        self.assertEqual(mappings.count(), 0)

    def test_user_updated_event_no_connection_found(self):
        """Test user.updated event when no ZoomOAuthConnection exists for the user."""
        event_data = {
            "event": "user.updated",
            "payload": {
                "object": {"id": "unknown_user", "pmi": "5551234567"},
                "old_object": {"id": "unknown_user", "pmi": "5559876543"},
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        # Should return 200 but not create mapping
        self.assertEqual(response.status_code, 200)

        # Verify no mapping was created
        mappings = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="5551234567")
        self.assertEqual(mappings.count(), 0)

    def test_invalid_signature(self):
        """Test webhook with invalid signature."""
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "123456789", "host_id": "test_user_123"},
                "operator_id": "test_user_123",
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        invalid_signature = "v0=invalid_signature_hash"

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=invalid_signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 400)

        # Verify no mapping was created
        mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="123456789").first()
        self.assertIsNone(mapping)

        # Verify last_unverified_webhook_received_at was updated
        self.zoom_oauth_app.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_app.last_unverified_webhook_received_at)
        self.assertIsNone(self.zoom_oauth_app.last_verified_webhook_received_at)

    def test_nonexistent_zoom_oauth_app(self):
        """Test webhook for non-existent ZoomOAuthApp."""
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "123456789", "host_id": "test_user_123"},
                "operator_id": "test_user_123",
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        # Use non-existent object_id
        invalid_url = reverse("external_webhooks:external-webhook-zoom-oauth-app", kwargs={"object_id": "zoa_nonexistent"})

        response = self.client.post(
            invalid_url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 400)

    def test_unknown_event_type(self):
        """Test webhook with unknown event type (should return 200 and do nothing)."""
        event_data = {
            "event": "unknown.event.type",
            "payload": {
                "object": {"id": "123456789"},
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        # Should return 200 (webhook received) but not process anything
        self.assertEqual(response.status_code, 200)

        # Verify last_verified_webhook_received_at was updated (signature was valid)
        self.zoom_oauth_app.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_app.last_verified_webhook_received_at)
        self.assertIsNone(self.zoom_oauth_app.last_unverified_webhook_received_at)

    def test_missing_signature_headers(self):
        """Test webhook with missing signature headers."""
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "123456789", "host_id": "test_user_123"},
                "operator_id": "test_user_123",
            },
        }
        body = json.dumps(event_data)

        # Send without signature headers
        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    @patch("bots.external_webhooks_views._upsert_zoom_meeting_to_zoom_oauth_connection_mapping")
    def test_meeting_created_event_calls_upsert_correctly(self, mock_upsert):
        """Test that meeting.created event calls upsert with correct parameters."""
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "123456789", "host_id": "test_user_123"},
                "operator_id": "test_user_123",
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 200)

        # Verify upsert was called with correct parameters
        mock_upsert.assert_called_once()
        call_args = mock_upsert.call_args[0]
        self.assertEqual(call_args[0], ["123456789"])  # meeting_ids
        self.assertEqual(call_args[1], self.zoom_oauth_connection)  # zoom_oauth_connection

    @patch("bots.external_webhooks_views._upsert_zoom_meeting_to_zoom_oauth_connection_mapping")
    def test_user_updated_event_calls_upsert_correctly(self, mock_upsert):
        """Test that user.updated event calls upsert with correct parameters."""
        event_data = {
            "event": "user.updated",
            "payload": {
                "object": {"id": "test_user_123", "pmi": "5551234567"},
                "old_object": {"id": "test_user_123", "pmi": "5559876543"},
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 200)

        # Verify upsert was called with correct parameters
        mock_upsert.assert_called_once()
        call_args = mock_upsert.call_args[0]
        self.assertEqual(call_args[0], ["5551234567"])  # meeting_ids (new PMI)
        self.assertEqual(call_args[1], self.zoom_oauth_connection)  # zoom_oauth_connection

    def test_meeting_created_updates_existing_mapping(self):
        """Test that meeting.created can update an existing mapping from another connection."""
        # Create another connection and mapping
        other_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            user_id="other_user_456",
            account_id="other_account_id",
        )
        ZoomMeetingToZoomOAuthConnectionMapping.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            zoom_oauth_connection=other_connection,
            meeting_id="123456789",
        )

        # Now send webhook for the same meeting with our user
        event_data = {
            "event": "meeting.created",
            "payload": {
                "object": {"id": "123456789", "host_id": "test_user_123"},
                "operator_id": "test_user_123",
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 200)

        # Verify mapping now points to our connection
        mapping = ZoomMeetingToZoomOAuthConnectionMapping.objects.get(meeting_id="123456789")
        self.assertEqual(mapping.zoom_oauth_connection, self.zoom_oauth_connection)

        # Verify there's only one mapping (not duplicated)
        mappings_count = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(meeting_id="123456789").count()
        self.assertEqual(mappings_count, 1)

    def test_endpoint_url_validation_event(self):
        """Test successful handling of endpoint.url_validation event."""
        plain_token = "qgg8vlvZRS6UYooatFL8Aw"
        event_data = {
            "event": "endpoint.url_validation",
            "payload": {
                "plainToken": plain_token,
            },
        }
        body = json.dumps(event_data)
        timestamp = "1234567890"
        signature = self._generate_zoom_signature(body, timestamp, "test_webhook_secret")

        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_ZM_SIGNATURE=signature,
            HTTP_X_ZM_REQUEST_TIMESTAMP=timestamp,
        )

        self.assertEqual(response.status_code, 200)

        # Verify the response contains the correct JSON structure
        response_data = json.loads(response.content)
        self.assertIn("plainToken", response_data)
        self.assertIn("encryptedToken", response_data)
        self.assertEqual(response_data["plainToken"], plain_token)

        # Verify the encryptedToken is correctly computed using HMAC SHA-256
        expected_encrypted_token = hmac.new("test_webhook_secret".encode("utf-8"), plain_token.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(response_data["encryptedToken"], expected_encrypted_token)

        # Verify last_verified_webhook_received_at was updated
        self.zoom_oauth_app.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_app.last_verified_webhook_received_at)
        self.assertIsNone(self.zoom_oauth_app.last_unverified_webhook_received_at)
