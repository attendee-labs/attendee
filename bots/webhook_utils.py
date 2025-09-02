import base64
import hashlib
import hmac
import json
import logging
import uuid

logger = logging.getLogger(__name__)


def trigger_webhook(webhook_trigger_type, bot=None, calendar=None, payload=None):
    """
    Trigger a webhook for a given event.
    Prioritizes bot-level webhook subscriptions over project-level ones.
    """
    from bots.models import WebhookDeliveryAttempt

    if bot:
        project = bot.project
    elif calendar:
        project = calendar.project
    else:
        raise ValueError("Either bot or calendar must be provided")

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

    delivery_attempts = []
    for subscription in subscriptions:
        # Create a webhook delivery attempt record
        delivery_attempt = WebhookDeliveryAttempt.objects.create(
            webhook_subscription=subscription,
            webhook_trigger_type=webhook_trigger_type,
            idempotency_key=uuid.uuid4(),
            bot=bot,
            calendar=calendar,
            payload=payload,
        )
        delivery_attempts.append(delivery_attempt)

        from bots.tasks.deliver_webhook_task import deliver_webhook

        deliver_webhook.delay(delivery_attempt.id)

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
