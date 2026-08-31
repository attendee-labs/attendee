"""
Read side of the per-bot resource snapshots written by
bots.bot_controller.bot_resource_snapshot_taker.BotResourceSnapshotTaker.

Each snapshot is one point-in-time sample of a single bot's resource usage (RAM,
CPU, per-process memory, database/redis connection counts and network throughput).
This module turns those raw snapshots into the context the BotResourceUsage
dashboard renders, so that resource usage can be compared across every bot.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import Aggregate, FloatField, Max
from django.db.models.functions import Cast
from django.utils import timezone

from .models import BotResourceSnapshot, RecordingTypes

# Windows the dashboard offers.
WINDOWS = {
    "1h": {"label": "1 hour", "hours": 1},
    "6h": {"label": "6 hours", "hours": 6},
    "24h": {"label": "24 hours", "hours": 24},
    "7d": {"label": "7 days", "hours": 24 * 7},
}
DEFAULT_WINDOW = "24h"

# Meeting type filters. The substring is matched against the bot's meeting_url.
# (Duplicated from bots.usage_utils.PLATFORM_FILTERS on purpose so this module
# stays self-contained.)
PLATFORM_FILTERS = {
    "zoom": "zoom.us",
    "meet": "meet.google.com",
    "teams": "teams.",
}
PLATFORM_OPTIONS = [
    {"key": "", "label": "All"},
    {"key": "zoom", "label": "Zoom"},
    {"key": "teams", "label": "Teams"},
    {"key": "meet", "label": "Google Meet"},
]

# Recording type filters. Mirrors bots.models.RecordingTypes and filters on the
# bot's default recording.
RECORDING_FILTERS = {
    "audio_and_video": RecordingTypes.AUDIO_AND_VIDEO,
    "audio_only": RecordingTypes.AUDIO_ONLY,
    "no_recording": RecordingTypes.NO_RECORDING,
}
RECORDING_OPTIONS = [
    {"key": "", "label": "All"},
    {"key": "audio_and_video", "label": "Video + Audio"},
    {"key": "audio_only", "label": "Audio Only"},
    {"key": "no_recording", "label": "No Recording"},
]


class PercentileCont(Aggregate):
    function = "PERCENTILE_CONT"
    template = "%(function)s(%(percentile)s) WITHIN GROUP (ORDER BY %(expressions)s)"

    def __init__(self, expression, percentile, **extra):
        super().__init__(
            expression,
            percentile=percentile,
            output_field=FloatField(),
            **extra,
        )


def user_can_view_bot_resource_usage(user):
    """Whether the bot resource usage dashboard exists for this user.

    There is nothing to show unless resource snapshots are being written, and
    deployments can narrow the dashboard to superusers even though the rest of the
    project pages are open to any admin.
    """
    if not settings.SAVE_BOT_RESOURCE_SNAPSHOTS:
        return False

    return user.is_superuser or not settings.BOT_RESOURCE_USAGE_ONLY_VIEWABLE_BY_SUPERUSERS


def _per_bot_stats(snapshots_qs, data_key):
    """Compute the per-bot maximum of data[data_key], then the max/p95/p99 across
    those per-bot maxima. Returns a dict with keys max, p95, p99 (values may be None
    when there are no snapshots).
    """
    per_bot = snapshots_qs.annotate(value=Cast(f"data__{data_key}", FloatField())).values("bot_id").annotate(max_value=Max("value"))
    return per_bot.aggregate(
        max=Max("max_value"),
        p99=PercentileCont("max_value", 0.99),
        p95=PercentileCont("max_value", 0.95),
    )


def get_bot_resource_usage_data(project, window=DEFAULT_WINDOW, platform="", recording=""):
    """Return the template context for the bot resource usage dashboard.

    Applies the selected time window, meeting type (platform) and recording type
    filters, then reports max/p95/p99 of the per-bot peak CPU and RAM usage.
    """
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    if platform not in PLATFORM_FILTERS:
        platform = ""
    if recording not in RECORDING_FILTERS:
        recording = ""

    now = timezone.now()
    cutoff = now - timedelta(hours=WINDOWS[window]["hours"])

    snapshots_qs = BotResourceSnapshot.objects.filter(bot__project=project, created_at__gte=cutoff)

    if platform:
        snapshots_qs = snapshots_qs.filter(bot__meeting_url__icontains=PLATFORM_FILTERS[platform])

    if recording:
        snapshots_qs = snapshots_qs.filter(
            bot__recordings__is_default_recording=True,
            bot__recordings__recording_type=RECORDING_FILTERS[recording],
        )

    latest_snapshot_at = snapshots_qs.order_by("-created_at").values_list("created_at", flat=True).first()

    cpu_stats = _per_bot_stats(snapshots_qs, "cpu_usage_millicores")
    ram_stats = _per_bot_stats(snapshots_qs, "ram_usage_megabytes")

    return {
        "window": window,
        "window_label": WINDOWS[window]["label"],
        "window_options": [{"key": key, "label": options["label"]} for key, options in WINDOWS.items()],
        "platform": platform,
        "platform_options": PLATFORM_OPTIONS,
        "recording": recording,
        "recording_options": RECORDING_OPTIONS,
        "snapshot_count": snapshots_qs.count(),
        "latest_snapshot_at": latest_snapshot_at,
        "cpu_stats": cpu_stats,
        "ram_stats": ram_stats,
    }
