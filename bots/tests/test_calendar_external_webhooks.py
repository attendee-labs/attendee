import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Organization
from bots.models import (
    Calendar,
    CalendarNotificationChannel,
    CalendarPlatform,
    Project,
)


class TestGoogleCalendarWebhooks(TestCase):
    """Test the Google Calendar webhook events."""

    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="test_client_id",
        )
        self.notification_channel = CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="test_channel_123",
            unique_key="first_channel_" + self.calendar.object_id,
            expires_at=timezone.now() + timedelta(days=7),
            raw={"test": "data"},
        )
        self.url = reverse("external_webhooks:external-webhook-google-calendar")

    def test_google_webhook_success(self):
        """Test successful handling of Google Calendar webhook notification."""
        response = self.client.post(
            self.url,
            data="",
            content_type="application/json",
            HTTP_X_GOOG_CHANNEL_ID="test_channel_123",
            HTTP_X_GOOG_RESOURCE_STATE="exists",
        )

        self.assertEqual(response.status_code, 200)

        # Verify notification_last_received_at was updated
        self.notification_channel.refresh_from_db()
        self.assertIsNotNone(self.notification_channel.notification_last_received_at)

        # Verify sync_task_requested_at was updated on the calendar
        self.calendar.refresh_from_db()
        self.assertIsNotNone(self.calendar.sync_task_requested_at)

    def test_google_webhook_sync_resource_state_ignored(self):
        """Test that Google webhook with resource state 'sync' is ignored."""
        response = self.client.post(
            self.url,
            data="",
            content_type="application/json",
            HTTP_X_GOOG_CHANNEL_ID="test_channel_123",
            HTTP_X_GOOG_RESOURCE_STATE="sync",
        )

        self.assertEqual(response.status_code, 200)

        # Verify notification_last_received_at was NOT updated (sync events are ignored)
        self.notification_channel.refresh_from_db()
        self.assertIsNone(self.notification_channel.notification_last_received_at)

        # Verify sync_task_requested_at was NOT updated
        self.calendar.refresh_from_db()
        self.assertIsNone(self.calendar.sync_task_requested_at)

    def test_google_webhook_unknown_channel_id(self):
        """Test Google webhook with unknown channel ID returns 200 but does nothing."""
        response = self.client.post(
            self.url,
            data="",
            content_type="application/json",
            HTTP_X_GOOG_CHANNEL_ID="unknown_channel_id",
            HTTP_X_GOOG_RESOURCE_STATE="exists",
        )

        # Should return 200 (acknowledge receipt) but not process
        self.assertEqual(response.status_code, 200)

        # Verify our notification channel was not updated
        self.notification_channel.refresh_from_db()
        self.assertIsNone(self.notification_channel.notification_last_received_at)

    def test_google_webhook_missing_channel_id_header(self):
        """Test Google webhook with missing channel ID header."""
        response = self.client.post(
            self.url,
            data="",
            content_type="application/json",
            HTTP_X_GOOG_RESOURCE_STATE="exists",
        )

        # Should return 200 (acknowledge receipt) but not process
        self.assertEqual(response.status_code, 200)

        # Verify our notification channel was not updated
        self.notification_channel.refresh_from_db()
        self.assertIsNone(self.notification_channel.notification_last_received_at)

    def test_google_webhook_update_resource_state(self):
        """Test Google webhook with 'update' resource state."""
        response = self.client.post(
            self.url,
            data="",
            content_type="application/json",
            HTTP_X_GOOG_CHANNEL_ID="test_channel_123",
            HTTP_X_GOOG_RESOURCE_STATE="update",
        )

        self.assertEqual(response.status_code, 200)

        # Verify notification_last_received_at was updated
        self.notification_channel.refresh_from_db()
        self.assertIsNotNone(self.notification_channel.notification_last_received_at)

        # Verify sync_task_requested_at was updated
        self.calendar.refresh_from_db()
        self.assertIsNotNone(self.calendar.sync_task_requested_at)

    def test_google_webhook_multiple_channels_correct_one_updated(self):
        """Test that only the correct notification channel is updated when multiple exist."""
        # Create another channel for the same calendar
        other_channel = CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="other_channel_456",
            unique_key="test_channel_123",
            expires_at=timezone.now() + timedelta(days=14),
            raw={"test": "data"},
        )

        response = self.client.post(
            self.url,
            data="",
            content_type="application/json",
            HTTP_X_GOOG_CHANNEL_ID="test_channel_123",
            HTTP_X_GOOG_RESOURCE_STATE="exists",
        )

        self.assertEqual(response.status_code, 200)

        # Verify only the matched channel was updated
        self.notification_channel.refresh_from_db()
        self.assertIsNotNone(self.notification_channel.notification_last_received_at)

        other_channel.refresh_from_db()
        self.assertIsNone(other_channel.notification_last_received_at)


class TestMicrosoftCalendarWebhooks(TestCase):
    """Test the Microsoft Calendar webhook events."""

    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.MICROSOFT,
            client_id="test_client_id",
        )
        self.notification_channel = CalendarNotificationChannel.objects.create(
            calendar=self.calendar,
            platform_uuid="test_subscription_123",
            unique_key="notification_channel_" + self.calendar.object_id,
            expires_at=timezone.now() + timedelta(days=7),
            raw={"test": "data"},
        )
        self.url = reverse("external_webhooks:external-webhook-microsoft-calendar")

    def test_microsoft_webhook_validation_request(self):
        """Test Microsoft webhook validation request returns the validation token."""
        validation_token = "test_validation_token_abc123"
        url_with_token = f"{self.url}?validationToken={validation_token}"

        response = self.client.post(
            url_with_token,
            data="",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), validation_token)
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_microsoft_webhook_notification_success(self):
        """Test successful handling of Microsoft Calendar webhook notification."""
        payload = {
            "value": [
                {
                    "subscriptionId": "test_subscription_123",
                    "changeType": "created",
                    "resource": "me/events/event_id_123",
                }
            ]
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify notification_last_received_at was updated
        self.notification_channel.refresh_from_db()
        self.assertIsNotNone(self.notification_channel.notification_last_received_at)

        # Verify sync_task_requested_at was updated on the calendar
        self.calendar.refresh_from_db()
        self.assertIsNotNone(self.calendar.sync_task_requested_at)

    def test_microsoft_webhook_unknown_subscription_id(self):
        """Test Microsoft webhook with unknown subscription ID returns 200 but does nothing."""
        payload = {
            "value": [
                {
                    "subscriptionId": "unknown_subscription_id",
                    "changeType": "created",
                    "resource": "me/events/event_id_123",
                }
            ]
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        # Should return 200 (acknowledge receipt) but not process
        self.assertEqual(response.status_code, 200)

        # Verify our notification channel was not updated
        self.notification_channel.refresh_from_db()
        self.assertIsNone(self.notification_channel.notification_last_received_at)

    def test_microsoft_webhook_empty_notifications(self):
        """Test Microsoft webhook with empty notifications list."""
        payload = {"value": []}

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify notification channel was not updated
        self.notification_channel.refresh_from_db()
        self.assertIsNone(self.notification_channel.notification_last_received_at)

    def test_microsoft_webhook_missing_subscription_id(self):
        """Test Microsoft webhook with notification missing subscription ID."""
        payload = {
            "value": [
                {
                    "changeType": "created",
                    "resource": "me/events/event_id_123",
                    # No subscriptionId
                }
            ]
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        # Should return 200 (acknowledge receipt)
        self.assertEqual(response.status_code, 200)

        # Verify notification channel was not updated
        self.notification_channel.refresh_from_db()
        self.assertIsNone(self.notification_channel.notification_last_received_at)

    def test_microsoft_webhook_invalid_json(self):
        """Test Microsoft webhook with invalid JSON payload."""
        response = self.client.post(
            self.url,
            data="invalid json {{{",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_microsoft_webhook_multiple_notifications(self):
        """Test Microsoft webhook with multiple notifications in payload."""
        # Create another calendar and notification channel
        other_calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.MICROSOFT,
            client_id="other_client_id",
        )
        other_channel = CalendarNotificationChannel.objects.create(
            calendar=other_calendar,
            platform_uuid="other_subscription_456",
            unique_key="notification_channel_" + other_calendar.object_id,
            expires_at=timezone.now() + timedelta(days=7),
            raw={"test": "data"},
        )

        payload = {
            "value": [
                {
                    "subscriptionId": "test_subscription_123",
                    "changeType": "created",
                    "resource": "me/events/event_id_1",
                },
                {
                    "subscriptionId": "other_subscription_456",
                    "changeType": "updated",
                    "resource": "me/events/event_id_2",
                },
            ]
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify both notification channels were updated
        self.notification_channel.refresh_from_db()
        self.assertIsNotNone(self.notification_channel.notification_last_received_at)

        other_channel.refresh_from_db()
        self.assertIsNotNone(other_channel.notification_last_received_at)

        # Verify both calendars have sync requested
        self.calendar.refresh_from_db()
        self.assertIsNotNone(self.calendar.sync_task_requested_at)

        other_calendar.refresh_from_db()
        self.assertIsNotNone(other_calendar.sync_task_requested_at)

    def test_microsoft_webhook_updated_change_type(self):
        """Test Microsoft webhook with 'updated' change type."""
        payload = {
            "value": [
                {
                    "subscriptionId": "test_subscription_123",
                    "changeType": "updated",
                    "resource": "me/events/event_id_123",
                }
            ]
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify notification_last_received_at was updated
        self.notification_channel.refresh_from_db()
        self.assertIsNotNone(self.notification_channel.notification_last_received_at)

    def test_microsoft_webhook_deleted_change_type(self):
        """Test Microsoft webhook with 'deleted' change type."""
        payload = {
            "value": [
                {
                    "subscriptionId": "test_subscription_123",
                    "changeType": "deleted",
                    "resource": "me/events/event_id_123",
                }
            ]
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify notification_last_received_at was updated
        self.notification_channel.refresh_from_db()
        self.assertIsNotNone(self.notification_channel.notification_last_received_at)

    def test_microsoft_webhook_no_value_key(self):
        """Test Microsoft webhook with missing 'value' key in payload."""
        payload = {"someOtherKey": "someValue"}

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        # Should return 200 but log warning and not process
        self.assertEqual(response.status_code, 200)

        # Verify notification channel was not updated
        self.notification_channel.refresh_from_db()
        self.assertIsNone(self.notification_channel.notification_last_received_at)
