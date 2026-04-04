from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

from notifications.models import FCMDeviceToken, Notification

logger = logging.getLogger(__name__)

_INVALID_TOKEN_ERRORS = {"UNREGISTERED", "INVALID_ARGUMENT"}


def deliver_notification_fcm(notification_id: str) -> None:
    try:
        from firebase_admin import messaging

        from notifications.firebase_init import get_firebase_app

        app = get_firebase_app()
        if not app:
            logger.warning("FCM delivery skipped: Firebase app not initialized")
            return

        notification = Notification.objects.prefetch_related("recipients").filter(pk=notification_id).first()
        if not notification:
            logger.warning("FCM delivery skipped: notification %s not found", notification_id)
            return

        for recipient in notification.recipients.all():
            tokens = list(
                FCMDeviceToken.objects.filter(
                    is_active=True,
                ).filter(
                    Q(user_sub=recipient.recipient_sub)
                    | Q(user__object_id=recipient.recipient_sub)
                ).values_list("token", flat=True)
            )
            if not tokens:
                continue

            msg = messaging.MulticastMessage(
                tokens=tokens,
                notification=messaging.Notification(
                    title=notification.title,
                    body=notification.body or None,
                ),
                data={
                    "notification_id": str(notification.id),
                    "title": notification.title,
                    "body": notification.body,
                    "priority": notification.priority,
                    "source_service": notification.source_service,
                    "sender_sub": notification.sender_user_id,
                    "created_at": notification.created_at.isoformat(),
                },
            )
            resp = messaging.send_each_for_multicast(msg, app=app)

            invalid_tokens: list[str] = []
            for idx, result in enumerate(resp.responses):
                if not result.success and getattr(result.exception, "code", "") in _INVALID_TOKEN_ERRORS:
                    invalid_tokens.append(tokens[idx])

            if invalid_tokens:
                FCMDeviceToken.objects.filter(token__in=invalid_tokens).update(is_active=False)

            if resp.success_count > 0:
                recipient.delivered_at = timezone.now()
                recipient.save(update_fields=["delivered_at"])

    except Exception:
        logger.exception("FCM delivery failed for notification=%s", notification_id)
