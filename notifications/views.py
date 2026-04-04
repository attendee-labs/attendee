from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications import preferences as pref
from notifications.models import FCMDeviceToken, Notification, NotificationCategory, NotificationRecipient
from notifications.serializers import (
    FCMTokenRegisterSerializer,
    FCMTokenUnregisterSerializer,
    NotificationCategorySerializer,
    NotificationCreateSerializer,
    NotificationPreferenceSerializer,
    NotificationPreferenceUpdateSerializer,
    NotificationSerializer,
)
from notifications.services import NotificationService


class NotificationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class NotificationAPIView(APIView):
    def _get_user_id(self, request):
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            return getattr(user, "object_id", None) or str(user.id)
        return request.headers.get("X-User-Sub")


@extend_schema_view(
    get=extend_schema(
        summary="List notifications for current user",
        parameters=[
            OpenApiParameter("is_read", bool, required=False),
            OpenApiParameter("priority", str, required=False),
            OpenApiParameter("mark_as_seen", bool, required=False),
        ],
    ),
    post=extend_schema(summary="Create and send notification", request=NotificationCreateSerializer),
)
class NotificationListCreateView(NotificationAPIView):
    def get(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        qs = (
            Notification.objects.filter(recipients__recipient_sub=user_id)
            .select_related("category")
            .prefetch_related(
                Prefetch(
                    "recipients",
                    queryset=NotificationRecipient.objects.filter(recipient_sub=user_id),
                    to_attr="_prefetched_recipient",
                )
            )
            .distinct()
            .order_by("-created_at")
        )

        is_read = request.query_params.get("is_read")
        if is_read is not None:
            is_read_bool = is_read.lower() in ("true", "1", "yes")
            qs = qs.filter(recipients__recipient_sub=user_id, recipients__is_read=is_read_bool)

        priority = request.query_params.get("priority")
        if priority:
            qs = qs.filter(priority=priority)

        paginator = NotificationPagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = NotificationSerializer(page, many=True, context={"request_user_id": user_id})

        mark_as_seen = request.query_params.get("mark_as_seen", "true").lower() not in ("false", "0", "no")
        if mark_as_seen and page:
            ids = [n.id for n in page]
            NotificationRecipient.objects.filter(
                notification_id__in=ids,
                recipient_sub=user_id,
                is_read=False,
            ).update(is_read=True, read_at=timezone.now())

        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = NotificationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            notification = NotificationService.send_notification(
                title=data["title"],
                body=data.get("body", ""),
                recipient_subs=data["recipient_subs"],
                sender_sub=user_id,
                source_service=data.get("source_service", ""),
                metadata=data.get("metadata"),
                priority=data.get("priority", "normal"),
                channels=data.get("channels"),
                recipient_type=data.get("recipient_type", "user"),
                category_id=data.get("category_id"),
                category_key=data.get("category_key") or None,
            )
        except ValidationError as exc:
            return Response(exc.message_dict, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({"detail": "Failed to create notification."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if notification is None:
            return Response({"detail": "No recipients eligible for this notification."}, status=status.HTTP_204_NO_CONTENT)

        out = NotificationSerializer(notification, context={"request_user_id": user_id})
        return Response(out.data, status=status.HTTP_201_CREATED)


class NotificationUnreadCountView(NotificationAPIView):
    def get(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        count = NotificationRecipient.objects.filter(recipient_sub=user_id, is_read=False).count()
        return Response({"count": count}, status=status.HTTP_200_OK)


class NotificationMarkAllReadView(NotificationAPIView):
    def patch(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        updated = NotificationRecipient.objects.filter(recipient_sub=user_id, is_read=False).update(
            is_read=True,
            read_at=timezone.now(),
        )
        return Response({"updated_count": updated}, status=status.HTTP_200_OK)


class NotificationDetailView(NotificationAPIView):
    def get(self, request, notification_id):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        notif = Notification.objects.filter(id=notification_id, recipients__recipient_sub=user_id).select_related("category").first()
        if not notif:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        out = NotificationSerializer(notif, context={"request_user_id": user_id})
        return Response(out.data)

    def delete(self, request, notification_id):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        deleted, _ = NotificationRecipient.objects.filter(notification_id=notification_id, recipient_sub=user_id).delete()
        if not deleted:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationMarkReadView(NotificationAPIView):
    def patch(self, request, notification_id):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        updated = NotificationRecipient.objects.filter(
            notification_id=notification_id,
            recipient_sub=user_id,
        ).update(is_read=True, read_at=timezone.now())
        if not updated:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"detail": "Marked as read."}, status=status.HTTP_200_OK)


class NotificationCategoryListView(NotificationAPIView):
    def get(self, request):
        scope = request.query_params.get("scope", "")
        qs = NotificationCategory.objects.all().order_by("key")
        if scope == "team":
            qs = qs.filter(key__in=pref.TEAM_ALLOWED_CATEGORY_KEYS)
        elif scope == "user":
            qs = qs.filter(key__in=pref.USER_ALLOWED_CATEGORY_KEYS)
        out = NotificationCategorySerializer(qs, many=True)
        return Response(out.data, status=status.HTTP_200_OK)


class UserNotificationPreferenceView(NotificationAPIView):
    def get(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        payload = pref.get_user_preference_payload(user_id)
        return Response(NotificationPreferenceSerializer(payload).data)

    def put(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = NotificationPreferenceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mode = serializer.validated_data["mode"]
        category_ids = serializer.validated_data.get("category_ids", [])

        if mode == "default":
            result = pref.replace_user_preferences(user_id, None)
        elif mode == "block_all":
            result = pref.replace_user_preferences(user_id, [])
        else:
            result = pref.replace_user_preferences(user_id, category_ids)

        return Response(NotificationPreferenceSerializer(result).data)

    def delete(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        result = pref.replace_user_preferences(user_id, None)
        return Response(NotificationPreferenceSerializer(result).data)


class TeamNotificationPreferenceView(NotificationAPIView):
    def get(self, request, team_id):
        payload = pref.get_team_preference_payload(team_id)
        return Response(NotificationPreferenceSerializer(payload).data)

    def put(self, request, team_id):
        serializer = NotificationPreferenceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mode = serializer.validated_data["mode"]
        category_ids = serializer.validated_data.get("category_ids", [])

        if mode == "default":
            result = pref.replace_team_preferences(team_id, None)
        elif mode == "block_all":
            result = pref.replace_team_preferences(team_id, [])
        else:
            result = pref.replace_team_preferences(team_id, category_ids)

        return Response(NotificationPreferenceSerializer(result).data)

    def delete(self, request, team_id):
        result = pref.replace_team_preferences(team_id, None)
        return Response(NotificationPreferenceSerializer(result).data)


class FCMTokenView(NotificationAPIView):
    @extend_schema(
        summary="Register an FCM device token",
        request=FCMTokenRegisterSerializer,
    )
    def post(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        auth_user = request.user if getattr(request.user, "is_authenticated", False) else None

        serializer = FCMTokenRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token = serializer.validated_data["token"]
        device_type = serializer.validated_data.get("device_type", FCMDeviceToken.DEVICE_WEB)

        _, created = FCMDeviceToken.objects.update_or_create(
            token=token,
            defaults={
                "user": auth_user,
                "user_sub": user_id,
                "device_type": device_type,
                "is_active": True,
            },
        )
        return Response(
            {"detail": "Token registered.", "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Unregister an FCM device token",
        request=FCMTokenUnregisterSerializer,
    )
    def delete(self, request):
        user_id = self._get_user_id(request)
        if not user_id:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        auth_user = request.user if getattr(request.user, "is_authenticated", False) else None

        serializer = FCMTokenUnregisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token = serializer.validated_data["token"]
        query = Q(token=token, user_sub=user_id)
        if auth_user is not None:
            query = Q(token=token) & (Q(user=auth_user) | Q(user_sub=user_id))
        updated = FCMDeviceToken.objects.filter(query).update(is_active=False)
        if updated:
            return Response({"detail": "Token unregistered."}, status=status.HTTP_200_OK)
        return Response({"detail": "Token not found."}, status=status.HTTP_404_NOT_FOUND)
