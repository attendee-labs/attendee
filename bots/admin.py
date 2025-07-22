import datetime
import os

from django.contrib import admin
from django.db import models
from django.db.models import Case, Count, ExpressionWrapper, F, FloatField, When
from django.db.models.functions import Extract
from django.utils import timezone
from django.utils.html import format_html

from .models import Bot, BotEvent, Utterance, WebhookDeliveryAttempt, WebhookSubscription


# Create an inline for BotEvent to show on the Bot admin page
class BotEventInline(admin.TabularInline):
    model = BotEvent
    extra = 0
    readonly_fields = ("created_at", "event_type", "event_sub_type", "old_state", "new_state", "metadata")
    can_delete = False
    max_num = 0  # Don't allow adding new events through admin
    ordering = ("created_at",)  # Show most recent events first

    def has_add_permission(self, request, obj=None):
        return False


class HasBotFilter(admin.SimpleListFilter):
    title = "has bot"
    parameter_name = "has_bot"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Has Bot"),
            ("no", "No Bot"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(bot__isnull=False)
        if self.value() == "no":
            return queryset.filter(bot__isnull=True)
        return queryset


@admin.register(BotEvent)
class BotEventAdmin(admin.ModelAdmin):
    list_display = ("bot_object_id", "event_type", "event_sub_type", "old_state", "new_state", "created_at")
    list_filter = ("event_type", "event_sub_type", "old_state", "new_state")
    search_fields = ("bot__object_id",)
    readonly_fields = ("bot", "created_at", "old_state", "new_state", "event_type", "event_sub_type", "metadata", "requested_bot_action_taken_at", "version")
    ordering = ("-created_at",)

    def bot_object_id(self, obj):
        return obj.bot.object_id

    bot_object_id.short_description = "Bot"
    bot_object_id.admin_order_field = "bot__object_id"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return True

    # Optional: organize fields in the detail view
    fieldsets = (
        ("Event Information", {"fields": ("bot", "event_type", "event_sub_type", "created_at")}),
        ("State Transition", {"fields": ("old_state", "new_state")}),
        ("Additional Data", {"fields": ("metadata", "requested_bot_action_taken_at")}),
        ("System", {"fields": ("version",)}),
    )


@admin.register(Bot)
class BotAdmin(admin.ModelAdmin):
    actions = None
    list_display = ("object_id", "name", "project", "state", "created_at", "updated_at", "view_logs_link")
    list_filter = ("state", "project")
    search_fields = ("object_id",)
    readonly_fields = ("object_id", "created_at", "updated_at", "state", "view_logs_link")
    inlines = [BotEventInline]  # Add the inline to the admin

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return True

    def view_logs_link(self, obj):
        pod_name = obj.k8s_pod_name()
        link_formatting_str = os.getenv("CLOUD_LOGS_LINK_FORMATTING_STR")
        if not link_formatting_str:
            return None
        try:
            url = link_formatting_str.format(pod_name=pod_name)
            return format_html('<a href="{}" target="_blank">View Logs</a>', url)
        except Exception:
            return None

    view_logs_link.short_description = "Cloud Logs"

    # Optional: if you want to organize the fields in the detail view
    fieldsets = (
        ("Basic Information", {"fields": ("object_id", "name", "project", "join_at", "deduplication_key")}),
        ("Meeting Details", {"fields": ("meeting_url", "meeting_uuid")}),
        ("Status", {"fields": ("state", "view_logs_link")}),
        ("Settings", {"fields": ("settings",)}),
        ("Metadata", {"fields": ("created_at", "updated_at", "version")}),
    )


@admin.register(WebhookDeliveryAttempt)
class WebhookDeliveryAttemptAdmin(admin.ModelAdmin):
    list_display = ("webhook_subscription", "webhook_trigger_type", "status", "attempt_count", "created_at", "last_attempt_at", "succeeded_at")
    list_filter = ("status", "webhook_trigger_type", "webhook_subscription")
    search_fields = ("webhook_subscription__url", "idempotency_key")
    readonly_fields = ("idempotency_key", "payload", "response_body_list")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return True

    def changelist_view(self, request, extra_context=None):
        # Get statistics
        stats = WebhookDeliveryAttempt.objects.aggregate(
            total=Count("id"),
            success=Count(Case(When(status=2, then=1))),  # 2 is SUCCESS
            failure=Count(Case(When(status=3, then=1))),  # 3 is FAILURE
            pending=Count(Case(When(status=1, then=1))),  # 1 is PENDING
        )

        total = stats["total"] or 0
        success = stats["success"] or 0
        failure = stats["failure"] or 0
        pending = stats["pending"] or 0

        # Calculate percentages
        success_pct = round((success / total) * 100, 2) if total > 0 else 0
        failure_pct = round((failure / total) * 100, 2) if total > 0 else 0
        pending_pct = round((pending / total) * 100, 2) if total > 0 else 0

        # Get latency statistics for the last 12 hours
        twelve_hours_ago = timezone.now() - datetime.timedelta(hours=12)

        # Query successful deliveries in the last 12 hours
        # Use extract('epoch') for PostgreSQL compatibility

        recent_deliveries = WebhookDeliveryAttempt.objects.filter(succeeded_at__isnull=False, created_at__gte=twelve_hours_ago).annotate(latency_seconds=ExpressionWrapper(Extract(F("succeeded_at") - F("created_at"), "epoch"), output_field=FloatField()))

        # Calculate latency statistics
        latency_stats = recent_deliveries.aggregate(recent_success_count=Count("id"), avg_latency=models.Avg("latency_seconds"), min_latency=models.Min("latency_seconds"), max_latency=models.Max("latency_seconds"))

        # Round latency values to 2 decimal places if they exist
        if latency_stats["avg_latency"] is not None:
            latency_stats["avg_latency"] = round(latency_stats["avg_latency"], 2)
        if latency_stats["min_latency"] is not None:
            latency_stats["min_latency"] = round(latency_stats["min_latency"], 2)
        if latency_stats["max_latency"] is not None:
            latency_stats["max_latency"] = round(latency_stats["max_latency"], 2)

        if not extra_context:
            extra_context = {}

        extra_context.update(
            {
                "webhook_stats": {
                    "total": total,
                    "success": success,
                    "failure": failure,
                    "pending": pending,
                    "success_pct": success_pct,
                    "failure_pct": failure_pct,
                    "pending_pct": pending_pct,
                    "recent_success_count": latency_stats["recent_success_count"],
                    "avg_latency": latency_stats["avg_latency"],
                    "min_latency": latency_stats["min_latency"],
                    "max_latency": latency_stats["max_latency"],
                }
            }
        )

        return super().changelist_view(request, extra_context=extra_context)


@admin.register(WebhookSubscription)
class WebhookSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("url", "project", "bot", "is_active", "created_at")
    list_filter = ("is_active", "project", HasBotFilter)
    search_fields = ("url", "project__name")
    readonly_fields = ("object_id",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return True


@admin.register(Utterance)
class UtteranceAdmin(admin.ModelAdmin):
    list_display = ("recording", "participant", "timestamp_ms", "duration_ms", "source", "created_at", "updated_at")
    list_filter = ("source", "audio_format")
    search_fields = ("participant__full_name", "recording__bot__object_id")
    readonly_fields = ("recording", "participant", "audio_blob", "audio_format", "timestamp_ms", "duration_ms", "source_uuid", "sample_rate", "source")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Get latency statistics for the last 12 hours
        twelve_hours_ago = timezone.now() - datetime.timedelta(hours=12)

        # Query successfully transcribed utterances in the last 12 hours
        # Exclude closed captions (source = 2 is CLOSED_CAPTION_FROM_PLATFORM)
        recent_utterances = Utterance.objects.filter(created_at__gte=twelve_hours_ago, transcription__isnull=False, source=Utterance.Sources.PER_PARTICIPANT_AUDIO).annotate(latency_seconds=ExpressionWrapper(Extract(F("updated_at") - F("created_at"), "epoch"), output_field=FloatField()))

        # Calculate latency statistics
        latency_stats = recent_utterances.aggregate(recent_count=Count("id"), avg_latency=models.Avg("latency_seconds"), min_latency=models.Min("latency_seconds"), max_latency=models.Max("latency_seconds"))

        # Round latency values to 2 decimal places if they exist
        if latency_stats["avg_latency"] is not None:
            latency_stats["avg_latency"] = round(latency_stats["avg_latency"], 2)
        if latency_stats["min_latency"] is not None:
            latency_stats["min_latency"] = round(latency_stats["min_latency"], 2)
        if latency_stats["max_latency"] is not None:
            latency_stats["max_latency"] = round(latency_stats["max_latency"], 2)

        if not extra_context:
            extra_context = {}

        extra_context.update(
            {
                "utterance_stats": {
                    "recent_count": latency_stats["recent_count"],
                    "avg_latency": latency_stats["avg_latency"],
                    "min_latency": latency_stats["min_latency"],
                    "max_latency": latency_stats["max_latency"],
                }
            }
        )

        return super().changelist_view(request, extra_context=extra_context)
