import os
from unittest.mock import MagicMock, patch

import stripe
from django.test import TestCase

from accounts.models import Organization
from bots.tasks.autopay_charge_task import autopay_charge, enqueue_autopay_charge_task


class AutopayChargeTaskTestCase(TestCase):
    def setUp(self):
        # Create test organization with autopay enabled
        self.org = Organization.objects.create(
            name="Test Org",
            centicredits=500,  # 5 credits, below default threshold of 10
            autopay_enabled=True,
            autopay_threshold_centricredits=1000,  # 10 credits
            autopay_amount_to_purchase_cents=5000,  # $50
            autopay_stripe_customer_id="cus_test123",
        )

        # Create test organization with autopay disabled
        self.org_disabled = Organization.objects.create(name="Test Org Disabled", centicredits=500, autopay_enabled=False, autopay_threshold_centricredits=1000, autopay_amount_to_purchase_cents=5000, autopay_stripe_customer_id="cus_test456")

        # Create test organization above threshold
        self.org_above_threshold = Organization.objects.create(
            name="Test Org Above Threshold",
            centicredits=1500,  # 15 credits, above threshold of 10
            autopay_enabled=True,
            autopay_threshold_centricredits=1000,  # 10 credits
            autopay_amount_to_purchase_cents=5000,  # $50
            autopay_stripe_customer_id="cus_test789",
        )

        # Create test organization without Stripe customer ID
        self.org_no_customer = Organization.objects.create(name="Test Org No Customer", centicredits=500, autopay_enabled=True, autopay_threshold_centricredits=1000, autopay_amount_to_purchase_cents=5000, autopay_stripe_customer_id=None)

    @patch("stripe.PaymentIntent.create")
    @patch("stripe.Customer.retrieve")
    def test_successful_autopay_charge(self, mock_customer_retrieve, mock_payment_intent_create):
        """Test successful autopay charge scenario"""
        # Mock Stripe customer with default payment method
        mock_customer = MagicMock()
        mock_customer.invoice_settings.default_payment_method = "pm_test123"
        mock_customer_retrieve.return_value = mock_customer

        # Mock successful payment intent
        mock_payment_intent = MagicMock()
        mock_payment_intent.id = "pi_test123"
        mock_payment_intent.status = "succeeded"
        mock_payment_intent_create.return_value = mock_payment_intent

        # Mock environment variable
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_key"}):
            # Execute task
            autopay_charge(self.org.id)

        # Verify Stripe calls
        mock_customer_retrieve.assert_called_once_with("cus_test123", api_key="sk_test_key")

        mock_payment_intent_create.assert_called_once_with(
            amount=5000,
            idempotency_key=None,
            currency="usd",
            customer="cus_test123",
            payment_method="pm_test123",
            off_session=True,
            confirm=True,
            description="Autopay charge for 100 Attendee credits",
            metadata={"organization_id": str(self.org.id), "credit_amount": "100", "autopay": "true"},
            api_key="sk_test_key",
        )

    def test_autopay_disabled_organization(self):
        """Test that autopay charge exits early for disabled organization"""
        with patch("stripe.Customer.retrieve") as mock_customer_retrieve:
            autopay_charge(self.org_disabled.id)

            # Stripe should not be called
            mock_customer_retrieve.assert_not_called()

    def test_organization_above_threshold(self):
        """Test that autopay charge exits early for organization above credit threshold"""
        with patch("stripe.Customer.retrieve") as mock_customer_retrieve:
            autopay_charge(self.org_above_threshold.id)

            # Stripe should not be called
            mock_customer_retrieve.assert_not_called()

    def test_missing_stripe_customer_id(self):
        """Test that autopay charge exits early for organization without Stripe customer ID"""
        with patch("stripe.Customer.retrieve") as mock_customer_retrieve:
            autopay_charge(self.org_no_customer.id)

            # Stripe should not be called
            mock_customer_retrieve.assert_not_called()

    @patch("stripe.Customer.retrieve")
    def test_missing_payment_method(self, mock_customer_retrieve):
        """Test handling of organization with no default payment method"""
        # Mock Stripe customer without default payment method
        mock_customer = MagicMock()
        mock_customer.invoice_settings.default_payment_method = None
        mock_customer_retrieve.return_value = mock_customer

        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_key"}):
            autopay_charge(self.org.id)

        # Refresh organization from database
        self.org.refresh_from_db()

        # Check that failure data was saved
        self.assertIsNotNone(self.org.autopay_charge_failure_data)
        self.assertEqual(self.org.autopay_charge_failure_data["error"], "No default payment method found")
        self.assertEqual(self.org.autopay_charge_failure_data["error_type"], "PaymentMethodNotFound")
        self.assertIn("timestamp", self.org.autopay_charge_failure_data)

    @patch("stripe.PaymentIntent.create")
    @patch("stripe.Customer.retrieve")
    def test_card_declined_error(self, mock_customer_retrieve, mock_payment_intent_create):
        """Test handling of card declined error"""
        # Mock Stripe customer with default payment method
        mock_customer = MagicMock()
        mock_customer.invoice_settings.default_payment_method = "pm_test123"
        mock_customer_retrieve.return_value = mock_customer

        # Mock card declined error
        card_error = stripe.error.CardError("Your card was declined.", "card_declined", "card_declined")
        card_error.error = MagicMock()
        card_error.error.message = "Your card was declined."
        card_error.error.code = "card_declined"
        card_error.error.type = "card_error"
        mock_payment_intent_create.side_effect = card_error

        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_key"}):
            autopay_charge(self.org.id)

        # Refresh organization from database
        self.org.refresh_from_db()

        # Check that failure data was saved
        self.assertIsNotNone(self.org.autopay_charge_failure_data)
        self.assertEqual(self.org.autopay_charge_failure_data["error"], "Card declined: Your card was declined.")
        self.assertEqual(self.org.autopay_charge_failure_data["error_type"], "CardDeclined")
        self.assertEqual(self.org.autopay_charge_failure_data["stripe_error_code"], "card_declined")
        self.assertEqual(self.org.autopay_charge_failure_data["stripe_error_type"], "card_error")

    @patch("stripe.PaymentIntent.create")
    @patch("stripe.Customer.retrieve")
    def test_payment_intent_failure(self, mock_customer_retrieve, mock_payment_intent_create):
        """Test handling of failed payment intent"""
        # Mock Stripe customer with default payment method
        mock_customer = MagicMock()
        mock_customer.invoice_settings.default_payment_method = "pm_test123"
        mock_customer_retrieve.return_value = mock_customer

        # Mock failed payment intent
        mock_payment_intent = MagicMock()
        mock_payment_intent.id = "pi_test123"
        mock_payment_intent.status = "requires_action"
        mock_payment_intent_create.return_value = mock_payment_intent

        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_key"}):
            autopay_charge(self.org.id)

        # Refresh organization from database
        self.org.refresh_from_db()

        # Check that failure data was saved
        self.assertIsNotNone(self.org.autopay_charge_failure_data)
        self.assertEqual(self.org.autopay_charge_failure_data["error"], "Payment intent failed with status: requires_action")
        self.assertEqual(self.org.autopay_charge_failure_data["error_type"], "PaymentFailed")
        self.assertEqual(self.org.autopay_charge_failure_data["payment_intent_id"], "pi_test123")
        self.assertEqual(self.org.autopay_charge_failure_data["payment_intent_status"], "requires_action")

    @patch("stripe.PaymentIntent.create")
    @patch("stripe.Customer.retrieve")
    def test_unexpected_error(self, mock_customer_retrieve, mock_payment_intent_create):
        """Test handling of unexpected errors"""
        # Mock Stripe customer with default payment method
        mock_customer = MagicMock()
        mock_customer.invoice_settings.default_payment_method = "pm_test123"
        mock_customer_retrieve.return_value = mock_customer

        # Mock unexpected error
        mock_payment_intent_create.side_effect = Exception("Unexpected Stripe error")

        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_key"}):
            with self.assertRaises(Exception) as context:
                autopay_charge(self.org.id)

            self.assertIn("Unexpected Stripe error", str(context.exception))

    @patch("bots.tasks.autopay_charge_task.autopay_charge.delay")
    def test_enqueue_autopay_charge_task(self, mock_delay):
        """Test enqueue_autopay_charge_task function"""
        # Ensure the organization doesn't have a task enqueued initially
        self.assertIsNone(self.org.autopay_charge_task_enqueued_at)

        # Call the enqueue function
        enqueue_autopay_charge_task(self.org)

        # Refresh organization from database
        self.org.refresh_from_db()

        # Check that the timestamp was set
        self.assertIsNotNone(self.org.autopay_charge_task_enqueued_at)

        # Check that the Celery task was queued
        mock_delay.assert_called_once_with(self.org.id)

    def test_organization_not_found(self):
        """Test handling of non-existent organization ID"""
        with self.assertRaises(Organization.DoesNotExist):
            autopay_charge(99999)  # Non-existent ID
