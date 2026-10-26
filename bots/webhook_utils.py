import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import uuid
from functools import partial
from urllib.parse import urlsplit

from django.db import transaction
from opentelemetry import context, trace

from attendee.telemetry import run_in_context, span_context_from, tag_current_span_with_bot, tracer

logger = logging.getLogger(__name__)


def trigger_webhook(webhook_trigger_type, bot=None, calendar=None, zoom_oauth_connection=None, payload=None):
    """
    Trigger a webhook for a given event.
    Prioritizes bot-level webhook subscriptions over project-level ones.
    """
    if bot:
        project = bot.project
    elif calendar:
        project = calendar.project
    elif zoom_oauth_connection:
        project = zoom_oauth_connection.zoom_oauth_app.project
    else:
        raise ValueError("Either bot or calendar or zoom_oauth_connection must be provided")

    if not payload:
        raise ValueError("Payload must be provided")

    # If bot was provided and has any bot-level webhook subscriptions, use those exclusively
    if bot and bot.bot_webhook_subscriptions.exists():
        subscriptions = bot.bot_webhook_subscriptions.filter(triggers__contains=[webhook_trigger_type], is_active=True)
    else:
        # Otherwise, fall back to project-level webhook subscriptions
        subscriptions = project.webhook_subscriptions.filter(
            bot__isnull=True,  # Only project-level (not bot-specific)
            triggers__contains=[webhook_trigger_type],
            is_active=True,
        )

    # A bot's webhooks are mostly triggered from the bot's own process, long after the request that
    # created it, so each trigger is linked to that request's trace rather than made part of it.
    launch_span_context = span_context_from(bot.trace_context) if bot else None
    with tracer().start_as_current_span("trigger_webhook", links=[trace.Link(launch_span_context)] if launch_span_context else []) as span:
        span.set_attribute("attendee.webhook.trigger", webhook_trigger_type)
        if bot:
            tag_current_span_with_bot(bot)
        return _create_delivery_attempts(subscriptions, webhook_trigger_type, bot, calendar, zoom_oauth_connection, payload)


def _create_delivery_attempts(subscriptions, webhook_trigger_type, bot, calendar, zoom_oauth_connection, payload):
    from bots.models import WebhookDeliveryAttempt
    from bots.tasks.deliver_webhook_task import deliver_webhook

    delivery_attempts = []
    for subscription in subscriptions:
        # Create a webhook delivery attempt record
        delivery_attempt = WebhookDeliveryAttempt.objects.create(
            webhook_subscription=subscription,
            webhook_trigger_type=webhook_trigger_type,
            idempotency_key=uuid.uuid4(),
            bot=bot,
            calendar=calendar,
            zoom_oauth_connection=zoom_oauth_connection,
            payload=payload,
        )
        delivery_attempts.append(delivery_attempt)

        # on_commit can run after the span has ended, so the task is enqueued under the context captured now
        transaction.on_commit(partial(run_in_context, context.get_current(), deliver_webhook.delay, delivery_attempt.id))

    return len(delivery_attempts)


def sign_payload(payload, secret):
    """
    Sign a webhook payload using HMAC-SHA256. Returns a base64-encoded HMAC-SHA256 signature
    """
    # Convert the payload to a canonical JSON string
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    # Create the signature
    signature = hmac.new(secret, payload_json.encode("utf-8"), hashlib.sha256).digest()

    # Return base64 encoded signature
    return base64.b64encode(signature).decode("utf-8")


def verify_signature(payload, signature, secret):
    """
    Verify a webhook signature. Not used in production, but useful for testing.
    """
    expected_signature = sign_payload(payload, secret)

    # Use constant-time comparison to prevent timing attacks
    return hmac.compare_digest(signature, expected_signature)


def url_is_public(url: str) -> bool:
    parsed = urlsplit(url)

    if not parsed.hostname:
        return False

    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return False

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])

        if not ip.is_global:
            return False

    return True
