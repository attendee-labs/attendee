import logging
import math
import uuid

from django.http import HttpResponse

from accounts.models import Organization, User

from .models import CreditTransaction, CreditTransactionManager

logger = logging.getLogger(__name__)


# Compute how many Attendee credits you get for a given purchase amount in dollars
def credit_amount_for_purchase_amount_dollars(purchase_amount_dollars):
    # Calculate credits based on tiered pricing
    if purchase_amount_dollars <= 200:
        # Tier 1: $0.50 per credit
        credit_amount = purchase_amount_dollars / 0.5
    elif purchase_amount_dollars <= 1000:
        # Tier 2: $0.40 per credit
        credit_amount = purchase_amount_dollars / 0.4
    else:
        # Tier 3: $0.35 per credit
        credit_amount = purchase_amount_dollars / 0.35

    # Floor the credit amount to ensure whole credits
    credit_amount = math.floor(credit_amount)

    # Ensure at least 1 credit
    if credit_amount < 1:
        credit_amount = 1

    return credit_amount


def process_customer_updated(customer, customer_previous_attributes):
    # Get the organization ID from the metadata
    organization_id = customer.metadata.get("organization_id")
    if not organization_id:
        logger.error("No organization ID found in customer metadata")
        return

    organization = Organization.objects.get(id=organization_id)
    if not organization:
        logger.error(f"Organization {organization_id} not found")
        return

    # Check if their payment method was updated
    logger.info(f"Customer {customer.id} updated")
    logger.info(f"Customer previous attributes: {customer_previous_attributes}")

    if organization.autopay_charge_failure_data is None:
        logger.info(f"Organization {organization.id} has no autopay charge failure data so not resetting")
        return

    if customer_previous_attributes.get("invoice_settings", {}).get("default_payment_method"):
        logger.info(f"Customer {customer.id} payment method updated so resetting autopay_charge_failure_data and autopay_charge_task_enqueued_at")
        organization.autopay_charge_failure_data = None
        organization.autopay_charge_task_enqueued_at = None
        organization.save()
        return

    logger.info(f"Customer {customer.id} payment method not updated so not doing anything")


def process_payment_intent_succeeded(payment_intent):
    if payment_intent.metadata.get("autopay") != "true":
        # This is not an autopay charge, so we don't need to do anything
        logger.info(f"Payment intent {payment_intent.id} is not an autopay charge, so we don't need to do anything")
        return

    # Get the organization ID from the metadata
    organization_id = payment_intent.metadata.get("organization_id")
    organization = Organization.objects.get(id=organization_id)
    if not organization:
        return HttpResponse("Invalid organization ID", status=400)

    # Get the credit amount from the metadata
    credit_amount = payment_intent.metadata.get("credit_amount")
    if not credit_amount:
        return HttpResponse("No credit amount provided", status=400)

    try:
        centicredits_delta = int(credit_amount) * 100
    except ValueError:
        return HttpResponse("Invalid credit amount format", status=400)

    if CreditTransaction.objects.filter(stripe_payment_intent_id=payment_intent.id).exists():
        logger.info(f"Payment intent {payment_intent.id} already processed")
        return HttpResponse(f"Payment intent {payment_intent.id} already processed", status=200)

    try:
        # Get payment amount in USD from checkout session (amount_total is in cents)
        amount_usd = payment_intent.amount / 100

        CreditTransactionManager.create_transaction(
            organization=organization,
            centicredits_delta=centicredits_delta,
            bot=None,
            stripe_payment_intent_id=payment_intent.id,
            description=f"Stripe payment of ${amount_usd:.2f} from Autopay",
        )
        organization.autopay_charge_failure_data = None
        organization.save()
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error creating credit transaction (error_id={error_id}): {e}")
        return HttpResponse(f"Error creating credit transaction. Error ID: {error_id}", status=400)


def process_checkout_session_completed(checkout_session):
    # Get the organization ID from the metadata
    organization_id = checkout_session.metadata.get("organization_id")
    organization = Organization.objects.get(id=organization_id)
    if not organization:
        return HttpResponse("Invalid organization ID", status=400)

    user_id = checkout_session.metadata.get("user_id")
    user = User.objects.get(id=user_id)
    if not user:
        return HttpResponse("Invalid user ID", status=400)

    if user.organization != organization:
        return HttpResponse("User does not belong to organization", status=400)

    # Get the credit amount from the metadata
    credit_amount = checkout_session.metadata.get("credit_amount")
    if not credit_amount:
        return HttpResponse("No credit amount provided", status=400)

    try:
        centicredits_delta = int(credit_amount) * 100
    except ValueError:
        return HttpResponse("Invalid credit amount format", status=400)

    if CreditTransaction.objects.filter(stripe_payment_intent_id=checkout_session.payment_intent).exists():
        logger.info(f"Payment intent {checkout_session.payment_intent} already processed")
        return HttpResponse(f"Payment intent {checkout_session.payment_intent} already processed", status=200)

    try:
        # Get payment amount in USD from checkout session (amount_total is in cents)
        amount_usd = checkout_session.amount_total / 100

        CreditTransactionManager.create_transaction(
            organization=organization,
            centicredits_delta=centicredits_delta,
            bot=None,
            stripe_payment_intent_id=checkout_session.payment_intent,
            description=f"Stripe payment of ${amount_usd:.2f}",
        )
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error creating credit transaction (error_id={error_id}): {e}")
        return HttpResponse(f"Error creating credit transaction. Error ID: {error_id}", status=400)
