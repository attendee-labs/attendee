from __future__ import annotations

from rest_framework import serializers

from notifications.models import (
    FCMDeviceToken,
    Notification,
    NotificationCategory,
    NotificationPriority,
    NotificationRecipient,
    RecipientType,
)


class NotificationCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationCategory
        fields = ["id", "key", "label", "description"]


class NotificationCreateSerializer(serializers.Serializer):
    recipient_subs = serializers.ListField(child=serializers.CharField(max_length=64), min_length=1)
    title = serializers.CharField(max_length=500)
    body = serializers.CharField(required=False, allow_blank=True, default="")
    source_service = serializers.CharField(required=False, allow_blank=True, default="")
    metadata = serializers.DictField(required=False, default=dict)
    priority = serializers.ChoiceField(choices=NotificationPriority.choices, required=False, default=NotificationPriority.NORMAL)
    channels = serializers.ListField(child=serializers.CharField(max_length=20), required=False, default=["fcm"])
    recipient_type = serializers.ChoiceField(choices=RecipientType.choices, required=False, default=RecipientType.USER)
    category_id = serializers.UUIDField(required=False, allow_null=True)
    category_key = serializers.CharField(required=False, allow_blank=True, default="")


class NotificationRecipientReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationRecipient
        fields = ["is_read", "read_at", "delivered_at"]


class NotificationSerializer(serializers.ModelSerializer):
    category = serializers.SerializerMethodField()
    is_read = serializers.SerializerMethodField()
    read_at = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "sender_user_id",
            "source_service",
            "title",
            "body",
            "metadata",
            "priority",
            "channels",
            "category",
            "created_at",
            "is_read",
            "read_at",
        ]

    def _recipient(self, obj: Notification):
        request_user_id = self.context.get("request_user_id")
        if not request_user_id:
            return None
        return obj.recipients.filter(recipient_sub=request_user_id).first()

    def get_category(self, obj: Notification):
        if not obj.category:
            return None
        return {
            "id": obj.category.id,
            "key": obj.category.key,
            "label": obj.category.label,
        }

    def get_is_read(self, obj: Notification):
        recipient = self._recipient(obj)
        return bool(recipient.is_read) if recipient else False

    def get_read_at(self, obj: Notification):
        recipient = self._recipient(obj)
        return recipient.read_at if recipient else None


class NotificationPreferenceUpdateSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["default", "block_all", "custom"])
    category_ids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True)


class NotificationPreferenceSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["default", "block_all", "custom"])
    category_ids = serializers.ListField(child=serializers.UUIDField())


class FCMTokenRegisterSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=4096)
    device_type = serializers.ChoiceField(
        choices=FCMDeviceToken.DEVICE_CHOICES,
        default=FCMDeviceToken.DEVICE_WEB,
    )


class FCMTokenUnregisterSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=4096)
