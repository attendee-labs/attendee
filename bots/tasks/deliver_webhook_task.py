import logging
import os
import time

import redis
import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from bots.models import SessionTypes, WebhookDeliveryAttempt, WebhookDeliveryAttemptStatus, WebhookTriggerTypes
from bots.redis_utils import incr_and_expire_nx
from bots.webhook_utils import sign_payload

logger = logging.getLogger(__name__)

_deliver_webhook_task_redis_client = None


# Create a singleton Redis client instance that will share connections across all tasks in the same process.
def get_deliver_webhook_task_redis_client():
    global _deliver_webhook_task_redis_client
    if _deliver_webhook_task_redis_client is None:
        _deliver_webhook_task_redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    return _deliver_webhook_task_redis_client


def is_global_webhook_rate_limit_reached():
    if not settings.GLOBAL_WEBHOOK_DELIVERIES_PER_SECOND_RATE_LIMIT:
        return False

    redis_client = get_deliver_webhook_task_redis_client()
    rate_limit_key = f"global_webhook_rate_limit:{int(time.time())}"
    count, _ = incr_and_expire_nx(redis_client, rate_limit_key, ttl=2)

    return count > settings.GLOBAL_WEBHOOK_DELIVERIES_PER_SECOND_RATE_LIMIT


# This is how many times we will try to deliver the webhook before giving up.
MAX_WEBHOOK_DELIVERY_ATTEMPTS = int(os.getenv("MAX_WEBHOOK_DELIVERY_ATTEMPTS", 3))
# This is how many times the task can be retried before giving up.
# This is distinct from MAX_WEBHOOK_DELIVERY_ATTEMPTS because the task can also be retried for
# reasons other than delivery failures (e.g., rate limiting enforced by Attendee via GLOBAL_WEBHOOK_DELIVERIES_PER_SECOND_RATE_LIMIT or unexpected exceptions).
DELIVER_WEBHOOK_TASK_MAX_RETRIES = int(os.getenv("DELIVER_WEBHOOK_TASK_MAX_RETRIES", MAX_WEBHOOK_DELIVERY_ATTEMPTS))


@shared_task(
    bind=True,
    retry_backoff=True,  # Enable exponential backoff
    max_retries=DELIVER_WEBHOOK_TASK_MAX_RETRIES,
    autoretry_for=(Exception,),
)
def deliver_webhook(self, delivery_id):
    """
    Deliver a webhook to its destination.
    """
    try:
        delivery = WebhookDeliveryAttempt.objects.get(id=delivery_id)
    except WebhookDeliveryAttempt.DoesNotExist:
        logger.error(f"Webhook delivery attempt {delivery_id} not found")
        raise  # Re-raises the original exception with preserved traceback

    subscription = delivery.webhook_subscription

    # If the subscription is no longer active, mark as failed and return
    if not subscription.is_active:
        delivery.status = WebhookDeliveryAttemptStatus.FAILURE
        error_response = {
            "status_code": None,  # No HTTP status since request failed
            "error_type": "InactiveSubscription",
            "error_message": "Webhook subscription is no longer active",
            "request_url": subscription.url,
        }
        delivery.add_to_response_body_list(error_response)
        delivery.save()
        return

    related_object_specific_webhook_data = {}

    if delivery.bot:
        if delivery.bot.session_type == SessionTypes.BOT:
            related_object_specific_webhook_data["bot_id"] = delivery.bot.object_id
            related_object_specific_webhook_data["bot_metadata"] = delivery.bot.metadata
        elif delivery.bot.session_type == SessionTypes.APP_SESSION:
            related_object_specific_webhook_data["app_session_id"] = delivery.bot.object_id
            related_object_specific_webhook_data["app_session_metadata"] = delivery.bot.metadata
    elif delivery.calendar:
        related_object_specific_webhook_data["calendar_id"] = delivery.calendar.object_id
        related_object_specific_webhook_data["calendar_deduplication_key"] = delivery.calendar.deduplication_key
        related_object_specific_webhook_data["calendar_metadata"] = delivery.calendar.metadata
    elif delivery.zoom_oauth_connection:
        related_object_specific_webhook_data["zoom_oauth_connection_id"] = delivery.zoom_oauth_connection.object_id
        related_object_specific_webhook_data["zoom_oauth_connection_metadata"] = delivery.zoom_oauth_connection.metadata
        related_object_specific_webhook_data["user_id"] = delivery.zoom_oauth_connection.user_id
        related_object_specific_webhook_data["account_id"] = delivery.zoom_oauth_connection.account_id

    # Prepare the webhook payload
    webhook_data = {
        "idempotency_key": str(delivery.idempotency_key),
        **related_object_specific_webhook_data,
        "trigger": WebhookTriggerTypes.trigger_type_to_api_code(delivery.webhook_trigger_type),
        "data": delivery.payload,
    }

    # Sign the payload
    active_secret = subscription.project.webhook_secrets.filter().order_by("-created_at").first()
    signature = sign_payload(webhook_data, active_secret.get_secret())

    # Check if the global webhook rate limit has been reached before delivering.
    if is_global_webhook_rate_limit_reached():
        retry_delay = int(os.getenv("GLOBAL_WEBHOOK_RATE_LIMIT_RETRY_DELAY_SECONDS", 3))
        logger.warning(
            "Global webhook deliveries per second rate limit of %s reached; retrying webhook delivery %s in %s seconds",
            settings.GLOBAL_WEBHOOK_DELIVERIES_PER_SECOND_RATE_LIMIT,
            delivery.id,
            retry_delay,
        )
        raise self.retry(
            exc=Exception("Retry due to global webhook rate limit"),
            countdown=retry_delay,
        )

    # Increment attempt counter
    delivery.attempt_count += 1
    delivery.last_attempt_at = timezone.now()

    # Send the webhook
    try:
        response = requests.post(
            subscription.url,
            json=webhook_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Attendee-Webhook/1.0",
                "X-Webhook-Signature": signature,
            },
            timeout=10,  # 10-second timeout
        )

        # Update the delivery attempt with the response
        delivery.response_status_code = response.status_code

        # Limit response body storage to prevent DB issues with large responses
        response_body = response.text[:1000]
        delivery.add_to_response_body_list(response_body)

        # Check if the delivery was successful (2xx status code)
        if 200 <= response.status_code < 300:
            delivery.status = WebhookDeliveryAttemptStatus.SUCCESS
            delivery.succeeded_at = timezone.now()
            delivery.save()
            return

        # If we got here, the delivery failed with a non-2xx status code
        delivery.status = WebhookDeliveryAttemptStatus.FAILURE

    except requests.RequestException as e:
        # Handle network errors, timeouts, etc.
        delivery.status = WebhookDeliveryAttemptStatus.FAILURE
        error_response = {
            "status_code": None,  # No HTTP status since request failed
            "error_type": type(e).__name__,
            "error_message": str(e),
            "request_url": subscription.url,
        }
        delivery.add_to_response_body_list(error_response)

    delivery.save()

    if delivery.status == WebhookDeliveryAttemptStatus.FAILURE:
        # Check if this was the last retry attempt
        if delivery.attempt_count >= MAX_WEBHOOK_DELIVERY_ATTEMPTS:
            logger.error(f"Webhook delivery failed after {delivery.attempt_count} attempts. " + f"Webhook ID: {delivery.id}, URL: {subscription.url}, " + f"Event: {delivery.webhook_trigger_type}, Status: {delivery.status}")
        else:
            logger.info(f"Retrying webhook delivery {delivery.id} (attempt {delivery.attempt_count}/{MAX_WEBHOOK_DELIVERY_ATTEMPTS})")
            raise Exception("Retry due to failure")
