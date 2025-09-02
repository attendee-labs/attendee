import logging
import os

import stripe
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from accounts.models import Organization
from bots.stripe_utils import credit_amount_for_purchase_amount_dollars

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    retry_backoff=True,  # Enable exponential backoff
    max_retries=3,
    autoretry_for=(stripe.error.RateLimitError, stripe.error.APIConnectionError, stripe.error.APIError, TimeoutError),
)
def autopay_charge(self, organization_id):
    """
    Process an autopay charge for an organization if they are below the threshold.
    """
    organization = Organization.objects.get(id=organization_id)
    logger.info(f"Processing autopay charge for organization {organization.id} ({organization.name})")

    # Check if autopay is enabled
    if not organization.autopay_enabled:
        logger.info(f"Autopay is disabled for organization {organization.id}")
        return

    # Check if organization has credits above the threshold
    if organization.centicredits >= organization.autopay_threshold_centricredits:
        logger.info(f"Organization {organization.id} has {organization.credits()} credits, above threshold of {organization.autopay_threshold_credits()} credits")
        return

    if not organization.autopay_stripe_customer_id:
        logger.info(f"No Stripe customer ID found for organization {organization.id}")
        return

    try:
        # Get the purchase amount in dollars
        purchase_amount_cents = organization.autopay_amount_to_purchase_cents
        purchase_amount_dollars = purchase_amount_cents / 100

        credit_amount = credit_amount_for_purchase_amount_dollars(purchase_amount_dollars)

        logger.info(f"Attempting to charge ${purchase_amount_dollars:.2f} for {credit_amount} credits to organization {organization.id}")

        # Fetch stripe customer
        customer = stripe.Customer.retrieve(
            organization.autopay_stripe_customer_id,
            api_key=os.getenv("STRIPE_SECRET_KEY"),
        )
        # Check if customer has a default payment method
        customer_default_payment_method = customer.invoice_settings.default_payment_method

        if not customer_default_payment_method:
            logger.error(f"No default payment method found for organization {organization.id}.")
            organization.autopay_charge_failure_data = {
                "error": "No default payment method found",
                "error_type": "PaymentMethodNotFound",
                "timestamp": timezone.now().isoformat(),
            }
            organization.save()
            return

        # Create payment intent with Stripe
        payment_intent = stripe.PaymentIntent.create(
            amount=purchase_amount_cents,
            currency="usd",
            customer=organization.autopay_stripe_customer_id,
            payment_method=customer_default_payment_method,
            off_session=True,
            confirm=True,
            description=f"Autopay charge for {credit_amount} Attendee credits",
            metadata={"organization_id": str(organization.id), "credit_amount": str(credit_amount), "autopay": "true"},
            api_key=os.getenv("STRIPE_SECRET_KEY"),
            idempotency_key=self.request.id,
        )

        # Check if payment was not successful
        if payment_intent.status == "succeeded":
            logger.info(f"Autopay charge successful for organization {organization.id}. Payment intent: {payment_intent.id}. Will rely on webhook to create credit transaction.")
            return
        else:
            error_msg = f"Payment intent failed with status: {payment_intent.status}"
            logger.error(f"Autopay charge failed for organization {organization.id}. Payment intent: {payment_intent.id}. Will retry.")
            organization.autopay_charge_failure_data = {
                "error": error_msg,
                "error_type": "PaymentFailed",
                "payment_intent_id": payment_intent.id,
                "payment_intent_status": payment_intent.status,
                "timestamp": timezone.now().isoformat(),
            }
            organization.save()
            return

    except stripe.error.CardError as e:
        # Card was declined
        error_msg = f"Card declined: {e.user_message or e.error.message}"
        logger.error(f"Organization {organization.id}: {error_msg}")
        organization.autopay_charge_failure_data = {
            "error": error_msg,
            "error_type": "CardDeclined",
            "stripe_error_code": e.error.code,
            "stripe_error_type": e.error.type,
            "timestamp": timezone.now().isoformat(),
        }
        organization.save()
        return

    except stripe.error.InvalidRequestError as e:
        # our params/Stripe state problem; don't retry blindly
        organization.autopay_charge_failure_data = {"error": str(e), "error_type": "InvalidRequest", "timestamp": timezone.now().isoformat()}
        organization.save()
        return

    except Exception as e:
        # Any other unexpected error
        error_msg = f"Unexpected error during autopay charge: {str(e)}"
        logger.error(f"Organization {organization.id}: {error_msg}")
        raise


def enqueue_autopay_charge_task(organization: Organization):
    """Enqueue a create autopay charge task for an organization."""
    with transaction.atomic():
        organization.autopay_charge_task_enqueued_at = timezone.now()
        organization.save()
        autopay_charge.delay(organization.id)
