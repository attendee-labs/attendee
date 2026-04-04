from __future__ import annotations

import logging
import threading

from django.db import transaction

from notifications.models import Notification, NotificationRecipient
from notifications.preferences import (
    filter_recipient_subs_by_user_preferences,
    resolve_category_by_key_or_id,
    team_allows_category,
)

logger = logging.getLogger(__name__)


class NotificationService:
    @classmethod
    def send_notification(
        cls,
        *,
        title: str,
        body: str = "",
        recipient_subs: list[str],
        sender_sub: str = "",
        source_service: str = "",
        metadata: dict | None = None,
        priority: str = "normal",
        channels: list[str] | None = None,
        recipient_type: str = "user",
        category_id=None,
        category_key=None,
        team_id: int | None = None,
    ):
        category_obj = resolve_category_by_key_or_id(category_id=category_id, category_key=category_key)

        if category_obj and team_id is not None and not team_allows_category(team_id, category_obj.id):
            return None

        if category_obj:
            recipient_subs = filter_recipient_subs_by_user_preferences(recipient_subs, category_obj.id)
            if not recipient_subs:
                return None

        with transaction.atomic():
            notification = Notification.objects.create(
                sender_user_id=sender_sub,
                source_service=source_service,
                title=title,
                body=body,
                metadata=metadata or {},
                priority=priority,
                channels=channels or ["fcm"],
                category=category_obj,
            )
            NotificationRecipient.objects.bulk_create(
                [
                    NotificationRecipient(
                        notification=notification,
                        recipient_sub=sub,
                        recipient_type=recipient_type,
                    )
                    for sub in recipient_subs
                ]
            )

            # Fire-and-forget push delivery (FCM only for now), without
            # blocking request latency.
            def _dispatch():
                try:
                    from notifications.delivery import deliver_notification_fcm

                    deliver_notification_fcm(str(notification.id))
                except Exception:
                    logger.exception("Notification dispatch failed for %s", notification.id)

            if (channels or ["fcm"]) and "fcm" in (channels or ["fcm"]):
                transaction.on_commit(lambda: threading.Thread(target=_dispatch, daemon=True).start())
        return notification
