from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class NotificationPriority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    URGENT = "urgent", "Urgent"


class RecipientType(models.TextChoices):
    USER = "user", "User"
    GROUP = "group", "Group"
    TEAM = "team", "Team"


class NotificationCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=100, unique=True)
    label = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.label or self.key


class UserNotificationPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=64, db_index=True)
    category = models.ForeignKey(
        NotificationCategory,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="user_preferences",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user_id", "category"],
                condition=Q(category__isnull=False),
                name="notifications_uniq_user_pref",
            ),
            models.UniqueConstraint(
                fields=["user_id"],
                condition=Q(category__isnull=True),
                name="notifications_uniq_user_block_all",
            ),
        ]


class TeamNotificationPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_id = models.IntegerField(db_index=True)
    category = models.ForeignKey(
        NotificationCategory,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="team_preferences",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team_id", "category"],
                condition=Q(category__isnull=False),
                name="notifications_uniq_team_pref",
            ),
            models.UniqueConstraint(
                fields=["team_id"],
                condition=Q(category__isnull=True),
                name="notifications_uniq_team_block_all",
            ),
        ]


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sender_user_id = models.CharField(max_length=64, blank=True, default="")
    source_service = models.CharField(max_length=200, blank=True, default="")
    title = models.CharField(max_length=500)
    body = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    priority = models.CharField(max_length=10, choices=NotificationPriority.choices, default=NotificationPriority.NORMAL)
    channels = models.JSONField(default=list, blank=True)
    category = models.ForeignKey(
        NotificationCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class NotificationRecipient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="recipients")
    recipient_sub = models.CharField(max_length=64, db_index=True)
    recipient_type = models.CharField(max_length=20, choices=RecipientType.choices, default=RecipientType.USER)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "recipient_sub"],
                name="notifications_uniq_notification_recipient",
            )
        ]
        indexes = [
            models.Index(fields=["recipient_sub", "is_read"]),
            models.Index(fields=["recipient_sub", "created_at"]),
        ]


class FCMDeviceToken(models.Model):
    DEVICE_WEB = "web"
    DEVICE_ANDROID = "android"
    DEVICE_IOS = "ios"
    DEVICE_CHOICES = [
        (DEVICE_WEB, "Web Browser"),
        (DEVICE_ANDROID, "Android"),
        (DEVICE_IOS, "iOS"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fcm_device_tokens",
    )
    user_sub = models.CharField(max_length=64, db_index=True)
    token = models.TextField(unique=True)
    device_type = models.CharField(max_length=20, choices=DEVICE_CHOICES, default=DEVICE_WEB)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user_sub", "is_active"]),
        ]
