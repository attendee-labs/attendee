import logging
import os

import stripe
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .stripe_utils import process_checkout_session_completed, process_payment_intent_succeeded

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class ExternalWebhookStripeView(View):
    """
    View to handle Stripe webhook events.
    This endpoint is called by Stripe when events occur (payments, refunds, etc.)
    """

    def post(self, request, *args, **kwargs):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

        if not sig_header:
            logger.error("Stripe signature header is missing")
            return HttpResponse(status=400)

        try:
            # Verify the webhook signature
            event = stripe.Webhook.construct_event(payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET"))

            # Handle different event types
            event_type = event["type"]
            event_data = event["data"]["object"]

            logger.info(f"Received Stripe webhook event: {event_type}")

            if event_type == "checkout.session.completed":
                # Payment was successful
                self._handle_checkout_session_completed(event_data)
            elif event_type == "payment_intent.succeeded":
                # Payment was successful
                self._handle_payment_intent_succeeded(event_data)
            else:
                logger.info(f"Received Stripe webhook event that we don't handle: {event_type}")

            return HttpResponse(status=200)

        except ValueError as e:
            # Invalid payload
            logger.error(f"Invalid Stripe payload: {str(e)}")
            return HttpResponse(status=400)
        except stripe.error.SignatureVerificationError as e:
            # Invalid signature
            logger.error(f"Invalid Stripe signature: {str(e)}")
            return HttpResponse(status=400)
        except Exception as e:
            # General error
            logger.error(f"Error processing Stripe webhook: {str(e)}")
            return HttpResponse(status=400)

    def _handle_checkout_session_completed(self, session):
        logger.info(f"Received Stripe webhook event for checkout session completed: {session}")

        process_checkout_session_completed(session)

    def _handle_payment_intent_succeeded(self, payment_intent):
        logger.info(f"Received Stripe webhook event for payment intent succeeded: {payment_intent}")

        process_payment_intent_succeeded(payment_intent)
