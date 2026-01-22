import json
import logging
import os
from datetime import timedelta

import stripe
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import CalendarNotificationChannel, ZoomOAuthApp, ZoomOAuthConnection
from .stripe_utils import process_checkout_session_completed, process_customer_updated, process_payment_intent_succeeded
from .zoom_oauth_connections_utils import _upsert_zoom_meeting_to_zoom_oauth_connection_mapping, _verify_zoom_webhook_signature, compute_zoom_webhook_validation_response

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class ExternalWebhookMicrosoftCalendarView(View):
    """
    View to handle Microsoft Calendar webhook events.
    This endpoint is called by Microsoft when events occur (calendar notifications, etc.)
    """

    def post(self, request, *args, **kwargs):
        logger.info(f"Received Microsoft Calendar webhook event. Headers: {request.headers} Body: {request.body}")

        # Handle validation request - Microsoft sends a validationToken parameter
        # when setting up the webhook subscription
        validation_token = request.GET.get("validationToken")
        if validation_token:
            logger.info(f"Received Microsoft Calendar webhook validation request with token: {validation_token}")
            return HttpResponse(validation_token, content_type="text/plain", status=200)

        # Parse the webhook payload
        try:
            body = json.loads(request.body)
            notifications = body.get("value", [])

            if not notifications:
                logger.warning("No notifications found in Microsoft Calendar webhook payload")
                return HttpResponse(status=200)

            # Process the first notification
            for notification in notifications:
                subscription_id = notification.get("subscriptionId")

                if not subscription_id:
                    logger.warning("No subscription ID found in Microsoft Calendar webhook notification")
                    return HttpResponse(status=200)

                # Look up the calendar notification channel by subscription ID
                calendar_notification_channel = CalendarNotificationChannel.objects.filter(platform_uuid=subscription_id).first()
                if not calendar_notification_channel:
                    logger.warning(f"No calendar notification channel found for subscription ID: {subscription_id}")
                    # TODO make request to stop the subscription in Microsoft Graph API
                    return HttpResponse(status=200)

                # Update the last received timestamp
                calendar_notification_channel.notification_last_received_at = timezone.now()
                calendar_notification_channel.save()

                # Request a sync task for the calendar. Don't request immediately to provide debouncing.
                calendar_notification_channel.calendar.sync_task_requested_at = timezone.now()
                calendar_notification_channel.calendar.save()

                logger.info(f"Requested sync task for calendar {calendar_notification_channel.calendar.object_id}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Microsoft Calendar webhook payload: {e}")
            return HttpResponse(status=400)
        except Exception as e:
            logger.exception(f"Error processing Microsoft Calendar webhook: {e}")
            return HttpResponse(status=500)

        return HttpResponse(status=200)


@method_decorator(csrf_exempt, name="dispatch")
class ExternalWebhookGoogleCalendarView(View):
    """
    View to handle Google Calendar webhook events.
    This endpoint is called by Google when events occur (calendar notifications, etc.)
    """

    def post(self, request, *args, **kwargs):
        logger.info(f"Received Google Calendar webhook event. Headers: {request.headers}")
        resource_state = request.headers.get("X-Goog-Resource-State")
        # If the resource state is sync, then this is just a notification that the channel is active. We don't need to do anything.
        if resource_state == "sync":
            logger.info("Ignoring Google Calendar webhook event because resource state is sync.")
            return HttpResponse(status=200)

        channel_id = request.headers.get("X-Goog-Channel-ID")
        calendar_notification_channel = CalendarNotificationChannel.objects.filter(platform_uuid=channel_id).first()
        if not calendar_notification_channel:
            logger.warning(f"No calendar notification channel found for channel ID: {channel_id}")
            # TODO make request to stop the channel in Google API
            return HttpResponse(status=200)

        calendar_notification_channel.notification_last_received_at = timezone.now()
        calendar_notification_channel.save()

        # Request a sync task for the calendar. Don't request immediately to provide debouncing.
        calendar_notification_channel.calendar.sync_task_requested_at = timezone.now()
        calendar_notification_channel.calendar.save()

        logger.info(f"Requested sync task for calendar {calendar_notification_channel.calendar.object_id}")

        return HttpResponse(status=200)


@method_decorator(csrf_exempt, name="dispatch")
class ExternalWebhookZoomOAuthAppView(View):
    """
    View to handle Zoom OAuth app webhook events.
    This endpoint is called by Zoom when events occur (oauth app created, etc.)
    """

    def post(self, request, object_id):
        request_body = request.body.decode("utf-8")
        signature_header = request.META.get("HTTP_X_ZM_SIGNATURE")
        timestamp_header = request.META.get("HTTP_X_ZM_REQUEST_TIMESTAMP")

        logger.info(f"Received Zoom OAuth app webhook event: {request_body}")

        try:
            zoom_oauth_app = ZoomOAuthApp.objects.get(object_id=object_id)
            if not _verify_zoom_webhook_signature(
                body=request_body,
                timestamp=timestamp_header,
                signature=signature_header,
                secret=zoom_oauth_app.webhook_secret,
            ):
                logger.error(f"Invalid Zoom webhook signature for webhook for zoom oauth app {zoom_oauth_app.object_id}")
                # Only update if it was more than 5 minutes ago to prevent excessive updates
                if not zoom_oauth_app.last_unverified_webhook_received_at or zoom_oauth_app.last_unverified_webhook_received_at < timezone.now() - timedelta(minutes=5):
                    zoom_oauth_app.last_unverified_webhook_received_at = timezone.now()
                    zoom_oauth_app.save()
                return HttpResponse(status=400)

            event_json = json.loads(request_body)
            event_type = event_json.get("event")
            if event_type == "meeting.created":
                meeting_id = event_json.get("payload", {}).get("object", {}).get("id")
                # Host is the user who is hosting the meeting
                host_id = event_json.get("payload", {}).get("object", {}).get("host_id")
                # Operator is the user who created the meeting
                operator_id = event_json.get("payload", {}).get("operator_id")
                # Just logging this to see if it ever happens
                if operator_id != host_id:
                    logger.info(f"Operator ID does not match Host ID. {operator_id} != {host_id}. This doesn't affect anything, but just logging it.")

                zoom_oauth_connection = ZoomOAuthConnection.objects.filter(zoom_oauth_app=zoom_oauth_app, user_id=operator_id).first()
                if not zoom_oauth_connection:
                    logger.info(f"No Zoom OAuth connection found for operator ID {operator_id}")
                    return HttpResponse(status=200)

                _upsert_zoom_meeting_to_zoom_oauth_connection_mapping([str(meeting_id)], zoom_oauth_connection)

            if event_type == "user.updated":
                new_object = event_json.get("payload", {}).get("object")
                old_object = event_json.get("payload", {}).get("old_object")
                if new_object.get("pmi") == old_object.get("pmi"):
                    logger.info(f"PMID did not change. {new_object.get('pmi')} == {old_object.get('pmi')}. So not doing anything.")
                    return HttpResponse(status=200)

                if new_object.get("pmi") is None:
                    logger.info("New PMI is None. So not doing anything.")
                    return HttpResponse(status=200)

                if new_object.get("id") is None:
                    logger.info("New user ID is None. So not doing anything.")
                    return HttpResponse(status=200)

                zoom_oauth_connection = ZoomOAuthConnection.objects.filter(zoom_oauth_app=zoom_oauth_app, user_id=new_object.get("id")).first()
                if not zoom_oauth_connection:
                    logger.info(f"No Zoom OAuth connection found for user ID {new_object.get('id')}")
                    return HttpResponse(status=200)

                _upsert_zoom_meeting_to_zoom_oauth_connection_mapping([str(new_object.get("pmi"))], zoom_oauth_connection)

            # Only update if it was more than 5 minutes ago to prevent excessive updates
            if not zoom_oauth_app.last_verified_webhook_received_at or zoom_oauth_app.last_verified_webhook_received_at < timezone.now() - timedelta(minutes=5):
                zoom_oauth_app.last_verified_webhook_received_at = timezone.now()
                zoom_oauth_app.save()

            # Handle endpoint.url_validation event type
            if event_type == "endpoint.url_validation":
                json_response = compute_zoom_webhook_validation_response(event_json.get("payload", {}).get("plainToken"), zoom_oauth_app.webhook_secret)
                logger.info(f"Received Zoom OAuth app webhook event for endpoint URL validation: {event_json}. Returning JSON response: {json_response}")
                return JsonResponse(json_response, status=200)

        except ZoomOAuthApp.DoesNotExist:
            logger.error("Zoom OAuth app does not exist")
            return HttpResponse(status=400)
        except Exception as e:
            logger.exception(f"Error processing Zoom OAuth app webhook: {e}")
            return HttpResponse(status=400)
        return HttpResponse(status=200)


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
            elif event_type == "customer.updated":
                # Customer updated
                event_previous_attributes = event["data"].get("previous_attributes")
                self._handle_customer_updated(event_data, event_previous_attributes)
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

    def _handle_customer_updated(self, customer, customer_previous_attributes):
        logger.info(f"Received Stripe webhook event for customer updated: {customer}")

        process_customer_updated(customer, customer_previous_attributes)
