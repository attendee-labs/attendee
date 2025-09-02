import json
import os
from unittest.mock import MagicMock, patch
from uuid import uuid4

import stripe
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import CreditTransaction, Project
from bots.stripe_utils import process_checkout_session_completed


class StripeBillingTestCase(TestCase):
    def setUp(self):
        # Create test org and user
        self.org = Organization.objects.create(name="Test Org", centicredits=1000)
        self.user = User.objects.create_user(username="testuser", email="test@example.com", password="testpass123", organization=self.org, role=UserRole.ADMIN)
        self.regular_user = User.objects.create_user(username="regularuser", email="regular@example.com", password="testpass123", organization=self.org, role=UserRole.REGULAR_USER)
        self.project = Project.objects.create(name="Test Project", organization=self.org)
        self.client = Client()
        self.client.force_login(self.user)

    @patch("stripe.checkout.Session.create")
    def test_create_checkout_session(self, mock_session_create):
        # Mock the Stripe checkout session response
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test-session"
        mock_session_create.return_value = mock_session

        # Test with a valid purchase amount
        response = self.client.post(reverse("bots:create-checkout-session", kwargs={"object_id": self.project.object_id}), {"purchase_amount": "50.0"})

        # Check that we got redirected to Stripe
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://checkout.stripe.com/test-session")

        # Verify Stripe was called correctly
        mock_session_create.assert_called_once()
        call_kwargs = mock_session_create.call_args.kwargs

        # Check line items
        self.assertEqual(call_kwargs["line_items"][0]["price_data"]["unit_amount"], 5000)  # $50.00 in cents
        self.assertEqual(call_kwargs["line_items"][0]["price_data"]["currency"], "usd")

        # Check metadata
        self.assertEqual(call_kwargs["metadata"]["organization_id"], str(self.org.id))
        self.assertEqual(call_kwargs["metadata"]["user_id"], str(self.user.id))
        self.assertEqual(call_kwargs["metadata"]["credit_amount"], "100")  # $50 at $0.50 per credit = 100 credits

    @patch("stripe.checkout.Session.create")
    def test_create_checkout_session_invalid_amounts(self, mock_session_create):
        # Test with an invalid amount (too high)
        response = self.client.post(
            reverse("bots:create-checkout-session", kwargs={"object_id": self.project.object_id}),
            {"purchase_amount": "15000.0"},  # $15,000 exceeds the $10,000 limit
        )
        self.assertEqual(response.status_code, 400)
        mock_session_create.assert_not_called()
        mock_session_create.reset_mock()

        # Test with a negative amount (should be corrected to minimum)
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/test-session"
        mock_session_create.return_value = mock_session

        response = self.client.post(reverse("bots:create-checkout-session", kwargs={"object_id": self.project.object_id}), {"purchase_amount": "-10.0"})
        self.assertEqual(response.status_code, 302)
        mock_session_create.assert_called_once()

        # Check it was corrected to the minimum amount
        call_kwargs = mock_session_create.call_args.kwargs
        # Should be at least $1 ($1 / $0.50 per credit = 2 credits)
        self.assertGreaterEqual(int(call_kwargs["metadata"]["credit_amount"]), 1)

    @patch("stripe.checkout.Session.retrieve")
    def test_checkout_success(self, mock_retrieve):
        # Mock the Stripe session response
        mock_session = MagicMock()
        mock_session.payment_intent = "pi_test123"
        mock_session.amount_total = 5000  # $50.00 in cents
        mock_session.metadata = {"organization_id": str(self.org.id), "user_id": str(self.user.id), "credit_amount": "100"}
        mock_retrieve.return_value = mock_session

        # Record initial credit balance
        initial_credits = self.org.centicredits

        # Test the success callback
        response = self.client.get(reverse("bots:checkout-success", kwargs={"object_id": self.project.object_id}), {"session_id": "cs_test123"})

        mock_retrieve.assert_called_once_with("cs_test123", api_key=os.getenv("STRIPE_SECRET_KEY"))

        # Check that a credit transaction was created with the correct values
        self.org.refresh_from_db()
        transaction = CreditTransaction.objects.filter(organization=self.org, stripe_payment_intent_id="pi_test123").first()

        self.assertIsNotNone(transaction, "Credit transaction should be created")
        self.assertEqual(transaction.centicredits_delta, 10000, "Transaction should add 100 credits (10000 centicredits)")
        self.assertEqual(transaction.centicredits_before, initial_credits, "Before credits should match initial balance")
        self.assertEqual(transaction.centicredits_after, initial_credits + 10000, "After credits should be initial + 10000")

        # Check we got redirected to billing page
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("bots:project-billing", kwargs={"object_id": self.project.object_id}))

    @patch("stripe.checkout.Session.retrieve")
    def test_checkout_success_error_handling(self, mock_retrieve):
        # Test missing session ID
        response = self.client.get(reverse("bots:checkout-success", kwargs={"object_id": self.project.object_id}))
        self.assertEqual(response.status_code, 400)
        mock_retrieve.assert_not_called()

        # Test Stripe API error
        mock_retrieve.side_effect = stripe.error.StripeError("Test Stripe error")
        response = self.client.get(reverse("bots:checkout-success", kwargs={"object_id": self.project.object_id}), {"session_id": "cs_test123"})
        self.assertEqual(response.status_code, 400)

    def test_process_checkout_session_completed(self):
        # Test the actual credit processing function
        initial_credits = self.org.centicredits

        # Create a mock Stripe session
        checkout_session = MagicMock()
        checkout_session.metadata = {"organization_id": str(self.org.id), "user_id": str(self.user.id), "credit_amount": "100"}
        checkout_session.payment_intent = f"pi_test_{uuid4()}"
        checkout_session.amount_total = 5000  # $50.00 in cents

        # Process the session
        process_checkout_session_completed(checkout_session)

        # Refresh org from database
        self.org.refresh_from_db()

        # Check credits were added (100 credits = 10000 centicredits)
        self.assertEqual(self.org.centicredits, initial_credits + 10000)

        # Check transaction was created
        transaction = CreditTransaction.objects.filter(organization=self.org, stripe_payment_intent_id=checkout_session.payment_intent).first()

        self.assertIsNotNone(transaction)
        self.assertEqual(transaction.centicredits_delta, 10000)
        self.assertEqual(transaction.centicredits_before, initial_credits)
        self.assertEqual(transaction.centicredits_after, initial_credits + 10000)

    def test_process_checkout_session_idempotence(self):
        # Test that processing the same session twice doesn't double-credit
        # Create a test payment intent ID
        payment_intent_id = f"pi_test_{uuid4()}"

        # Create a mock Stripe session
        checkout_session = MagicMock()
        checkout_session.metadata = {"organization_id": str(self.org.id), "user_id": str(self.user.id), "credit_amount": "100"}
        checkout_session.payment_intent = payment_intent_id
        checkout_session.amount_total = 5000  # $50.00 in cents

        # Process the session
        process_checkout_session_completed(checkout_session)
        self.org.refresh_from_db()
        initial_credits_after_first_process = self.org.centicredits
        initial_transaction_count = CreditTransaction.objects.count()

        # Process the same session again
        process_checkout_session_completed(checkout_session)

        # Refresh org and check credits haven't changed
        self.org.refresh_from_db()
        self.assertEqual(self.org.centicredits, initial_credits_after_first_process)

        # Check no new transaction was created
        self.assertEqual(CreditTransaction.objects.count(), initial_transaction_count)

    @patch("stripe.Webhook.construct_event")
    def test_stripe_checkout_session_completed_webhook(self, mock_construct_event):
        # Create a webhook client without CSRF protection
        webhook_client = Client(enforce_csrf_checks=False)

        # Create a proper MagicMock object instead of a dictionary
        mock_session = MagicMock()
        mock_session.id = "cs_test_webhook"
        mock_session.payment_intent = "pi_test_webhook"
        mock_session.metadata = {"organization_id": str(self.org.id), "user_id": str(self.user.id), "credit_amount": "100"}
        mock_session.amount_total = 5000  # $50.00 in cents

        # Mock the Stripe event
        mock_event = {"type": "checkout.session.completed", "data": {"object": mock_session}}

        mock_construct_event.return_value = mock_event

        # Record initial credit balance
        initial_credits = self.org.centicredits

        # Test the webhook endpoint
        response = webhook_client.post(
            reverse("external-webhook-stripe"),
            data=json.dumps({"type": "checkout.session.completed"}),  # Actual content not important due to mocking
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="test_signature",
        )

        # Check that the webhook processed successfully
        self.assertEqual(response.status_code, 200)
        mock_construct_event.assert_called_once()

        # Check that a credit transaction was created with the correct values
        self.org.refresh_from_db()
        transaction = CreditTransaction.objects.filter(organization=self.org, stripe_payment_intent_id="pi_test_webhook").first()

        self.assertIsNotNone(transaction, "Credit transaction should be created")
        self.assertEqual(transaction.centicredits_delta, 10000, "Transaction should add 100 credits (10000 centicredits)")
        self.assertEqual(transaction.centicredits_before, initial_credits, "Before credits should match initial balance")
        self.assertEqual(transaction.centicredits_after, initial_credits + 10000, "After credits should be initial + 10000")

    @patch("stripe.Webhook.construct_event")
    def test_stripe_payment_intent_succeeded_webhook(self, mock_construct_event):
        """Test payment intent succeeded webhook for autopay charges"""
        # Create a webhook client without CSRF protection
        webhook_client = Client(enforce_csrf_checks=False)

        # Create a proper MagicMock object for payment intent (autopay charge)
        mock_payment_intent = MagicMock()
        mock_payment_intent.id = "pi_test_autopay"
        mock_payment_intent.amount = 5000  # $50.00 in cents
        mock_payment_intent.metadata = {
            "organization_id": str(self.org.id),
            "credit_amount": "100",
            "autopay": "true",  # This marks it as an autopay charge
        }

        # Mock the Stripe event
        mock_event = {"type": "payment_intent.succeeded", "data": {"object": mock_payment_intent}}

        mock_construct_event.return_value = mock_event

        # Record initial credit balance
        initial_credits = self.org.centicredits

        # Test the webhook endpoint
        response = webhook_client.post(
            reverse("external-webhook-stripe"),
            data=json.dumps({"type": "payment_intent.succeeded"}),  # Actual content not important due to mocking
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="test_signature",
        )

        # Check that the webhook processed successfully
        self.assertEqual(response.status_code, 200)
        mock_construct_event.assert_called_once()

        # Check that a credit transaction was created with the correct values
        self.org.refresh_from_db()
        transaction = CreditTransaction.objects.filter(organization=self.org, stripe_payment_intent_id="pi_test_autopay").first()

        self.assertIsNotNone(transaction, "Credit transaction should be created")
        self.assertEqual(transaction.centicredits_delta, 10000, "Transaction should add 100 credits (10000 centicredits)")
        self.assertEqual(transaction.centicredits_before, initial_credits, "Before credits should match initial balance")
        self.assertEqual(transaction.centicredits_after, initial_credits + 10000, "After credits should be initial + 10000")

        # Verify the description indicates this was an autopay charge
        self.assertIn("Autopay", transaction.description)

        # Verify autopay failure data was cleared
        self.assertIsNone(self.org.autopay_charge_failure_data)

    @patch("stripe.Webhook.construct_event")
    def test_stripe_customer_updated_webhook_payment_method_changed(self, mock_construct_event):
        """Test customer.updated webhook when payment method is changed"""
        # Create a webhook client without CSRF protection
        webhook_client = Client(enforce_csrf_checks=False)

        # Set up organization with autopay failure data and task enqueued timestamp
        from django.utils import timezone

        self.org.autopay_stripe_customer_id = "cus_test123"
        self.org.autopay_charge_failure_data = {"error": "card_declined", "attempts": 2}
        self.org.autopay_charge_task_enqueued_at = timezone.now()
        self.org.save()

        # Create a mock customer with updated payment method
        mock_customer = MagicMock()
        mock_customer.id = "cus_test123"
        mock_customer.metadata = {"organization_id": str(self.org.id)}

        # Mock the Stripe event with previous_attributes indicating payment method change
        mock_event = {
            "type": "customer.updated",
            "data": {
                "object": mock_customer,
                "previous_attributes": {
                    "invoice_settings": {
                        "default_payment_method": "pm_old123"  # This indicates payment method changed
                    }
                },
            },
        }

        mock_construct_event.return_value = mock_event

        # Test the webhook endpoint
        response = webhook_client.post(
            reverse("external-webhook-stripe"),
            data=json.dumps({"type": "customer.updated"}),
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="test_signature",
        )

        # Check that the webhook processed successfully
        self.assertEqual(response.status_code, 200)
        mock_construct_event.assert_called_once()

        # Verify that autopay failure data and task timestamp were cleared
        self.org.refresh_from_db()
        self.assertIsNone(self.org.autopay_charge_failure_data)
        self.assertIsNone(self.org.autopay_charge_task_enqueued_at)

    @patch("stripe.Webhook.construct_event")
    def test_stripe_webhook_error_handling(self, mock_construct_event):
        webhook_client = Client(enforce_csrf_checks=False)

        # Test missing signature header
        response = webhook_client.post(reverse("external-webhook-stripe"), data=json.dumps({"type": "test"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        mock_construct_event.assert_not_called()

        # Test invalid signature
        mock_construct_event.side_effect = stripe.error.SignatureVerificationError("Invalid signature", "sig")
        response = webhook_client.post(reverse("external-webhook-stripe"), data=json.dumps({"type": "test"}), content_type="application/json", HTTP_STRIPE_SIGNATURE="invalid_signature")
        self.assertEqual(response.status_code, 400)

        # Test invalid payload
        mock_construct_event.side_effect = ValueError("Invalid payload")
        response = webhook_client.post(reverse("external-webhook-stripe"), data="invalid json", content_type="application/json", HTTP_STRIPE_SIGNATURE="test_signature")
        self.assertEqual(response.status_code, 400)

    # Tests for ProjectAutopayStripePortalView
    @patch("stripe.Customer.create")
    @patch("stripe.Customer.retrieve")
    @patch("stripe.billing_portal.Session.create")
    def test_stripe_portal_new_customer(self, mock_portal_create, mock_customer_retrieve, mock_customer_create):
        """Test creating a Stripe customer and billing portal for organization without existing customer"""
        # Mock Stripe customer creation
        mock_customer = MagicMock()
        mock_customer.id = "cus_test123"
        mock_customer_create.return_value = mock_customer

        # Mock customer retrieval (no default payment method)
        mock_retrieved_customer = MagicMock()
        mock_retrieved_customer.invoice_settings.default_payment_method = None
        mock_customer_retrieve.return_value = mock_retrieved_customer

        # Mock billing portal session creation
        mock_session = MagicMock()
        mock_session.url = "https://billing.stripe.com/test-session"
        mock_portal_create.return_value = mock_session

        # Make request
        response = self.client.post(reverse("bots:project-autopay-stripe-portal", kwargs={"object_id": self.project.object_id}))

        # Check that we got redirected to Stripe billing portal
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://billing.stripe.com/test-session")

        # Verify customer was created
        mock_customer_create.assert_called_once()
        call_kwargs = mock_customer_create.call_args.kwargs
        self.assertEqual(call_kwargs["email"], self.user.email)
        self.assertEqual(call_kwargs["name"], self.org.name)
        self.assertEqual(call_kwargs["metadata"]["organization_id"], str(self.org.id))

        # Verify organization was updated with customer ID
        self.org.refresh_from_db()
        self.assertEqual(self.org.autopay_stripe_customer_id, "cus_test123")

        # Verify billing portal session was created with flow_data for new payment method
        mock_portal_create.assert_called_once()
        portal_kwargs = mock_portal_create.call_args.kwargs
        self.assertEqual(portal_kwargs["customer"], "cus_test123")
        self.assertEqual(portal_kwargs["flow_data"], {"type": "payment_method_update"})

    @patch("stripe.Customer.retrieve")
    @patch("stripe.billing_portal.Session.create")
    def test_stripe_portal_existing_customer_with_payment_method(self, mock_portal_create, mock_customer_retrieve):
        """Test billing portal for organization with existing customer that has payment method"""
        # Set up organization with existing customer ID
        self.org.autopay_stripe_customer_id = "cus_existing123"
        self.org.save()

        # Mock customer retrieval (has default payment method)
        mock_retrieved_customer = MagicMock()
        mock_retrieved_customer.invoice_settings.default_payment_method = "pm_test123"
        mock_customer_retrieve.return_value = mock_retrieved_customer

        # Mock billing portal session creation
        mock_session = MagicMock()
        mock_session.url = "https://billing.stripe.com/existing-session"
        mock_portal_create.return_value = mock_session

        # Make request
        response = self.client.post(reverse("bots:project-autopay-stripe-portal", kwargs={"object_id": self.project.object_id}))

        # Check that we got redirected to Stripe billing portal
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://billing.stripe.com/existing-session")

        # Verify billing portal session was created without flow_data (no need to add payment method)
        mock_portal_create.assert_called_once()
        portal_kwargs = mock_portal_create.call_args.kwargs
        self.assertEqual(portal_kwargs["customer"], "cus_existing123")
        self.assertNotIn("flow_data", portal_kwargs)

    @patch("stripe.Customer.create")
    def test_stripe_portal_stripe_error(self, mock_customer_create):
        """Test error handling when Stripe API fails"""
        # Mock Stripe error
        mock_customer_create.side_effect = stripe.error.StripeError("Test Stripe error")

        # Make request
        response = self.client.post(reverse("bots:project-autopay-stripe-portal", kwargs={"object_id": self.project.object_id}))

        # Check error response
        self.assertEqual(response.status_code, 400)
        self.assertIn("Error setting up payment method", response.content.decode())

    def test_stripe_portal_regular_user_forbidden(self):
        """Test that regular users cannot access the Stripe portal view"""
        # Switch to regular user
        self.client.force_login(self.regular_user)

        # Make request
        response = self.client.post(reverse("bots:project-autopay-stripe-portal", kwargs={"object_id": self.project.object_id}))

        # Check that access is forbidden
        self.assertEqual(response.status_code, 403)

    # Tests for ProjectAutopayView
    def test_autopay_update_all_settings(self):
        """Test updating all autopay settings with valid data"""
        data = {
            "autopay_enabled": True,
            "autopay_threshold_credits": 50.0,
            "autopay_amount_dollars": 100.0,
        }

        response = self.client.patch(
            reverse("bots:project-autopay", kwargs={"object_id": self.project.object_id}),
            data=json.dumps(data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Autopay settings updated successfully")

        # Verify organization was updated
        self.org.refresh_from_db()
        self.assertTrue(self.org.autopay_enabled)
        self.assertEqual(self.org.autopay_threshold_centricredits, 5000)  # 50 credits * 100
        self.assertEqual(self.org.autopay_amount_to_purchase_cents, 10000)  # $100 * 100

    def test_autopay_invalid_threshold_credits(self):
        """Test validation for autopay_threshold_credits field"""
        # Test negative value
        data = {"autopay_threshold_credits": -10}
        response = self.client.patch(
            reverse("bots:project-autopay", kwargs={"object_id": self.project.object_id}),
            data=json.dumps(data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Credit threshold must be a positive number")

        # Test zero value
        data = {"autopay_threshold_credits": 0}
        response = self.client.patch(
            reverse("bots:project-autopay", kwargs={"object_id": self.project.object_id}),
            data=json.dumps(data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Credit threshold must be a positive number")

        # Test too high value
        data = {"autopay_threshold_credits": 15000}
        response = self.client.patch(
            reverse("bots:project-autopay", kwargs={"object_id": self.project.object_id}),
            data=json.dumps(data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Credit threshold cannot exceed 10,000 credits")

        # Test non-numeric value
        data = {"autopay_threshold_credits": "not_a_number"}
        response = self.client.patch(
            reverse("bots:project-autopay", kwargs={"object_id": self.project.object_id}),
            data=json.dumps(data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Credit threshold must be a positive number")

    def test_autopay_regular_user_forbidden(self):
        """Test that regular users cannot access the autopay PATCH view"""
        # Switch to regular user
        self.client.force_login(self.regular_user)

        data = {"autopay_enabled": True}
        response = self.client.patch(
            reverse("bots:project-autopay", kwargs={"object_id": self.project.object_id}),
            data=json.dumps(data),
            content_type="application/json",
        )

        # Check that access is forbidden
        self.assertEqual(response.status_code, 403)
