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
from django.db import connection
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
    per_bot = snapshots_qs.annotate(value=Cast(f"data__{data_key}", FloatField())).values("bot_id").annotate(bot_value=Max("value"))
    return per_bot.aggregate(
        max=Max("bot_value"),
        p99=PercentileCont("bot_value", 0.99),
        p95=PercentileCont("bot_value", 0.95),
    )


def _per_bot_percentile_stats(snapshots_qs, data_key, per_bot_percentile):
    """Like _per_bot_stats, but reduce each bot to a percentile of its samples
    (instead of its max) before taking the max/p95/p99 across bots.

    Using a per-bot percentile discards brief spikes: a bot that only touched a
    high value for a single sample is represented by its typical high usage
    rather than that one-off peak.
    """
    per_bot = snapshots_qs.annotate(value=Cast(f"data__{data_key}", FloatField())).values("bot_id").annotate(bot_value=PercentileCont("value", per_bot_percentile))
    return per_bot.aggregate(
        max=Max("bot_value"),
        p99=PercentileCont("bot_value", 0.99),
        p95=PercentileCont("bot_value", 0.95),
    )


def _top_bots_by_cpu_p99(snapshots_qs, limit=10):
    """Return the `limit` bots with the highest per-bot p99 CPU usage.

    Each bot is reduced to the p99 of its CPU samples (matching the "ignores brief
    spikes" card), then bots are ranked by that value. Returns a list of dicts with
    the bot's object_id, meeting_url and cpu_p99 (millicores).
    """
    per_bot = snapshots_qs.annotate(value=Cast("data__cpu_usage_millicores", FloatField())).values("bot_id", "bot__object_id", "bot__meeting_url").annotate(cpu_p99=PercentileCont("value", 0.99)).order_by("-cpu_p99")[:limit]
    return [
        {
            "object_id": row["bot__object_id"],
            "meeting_url": row["bot__meeting_url"],
            "cpu_p99": row["cpu_p99"],
        }
        for row in per_bot
    ]


def _cpu_by_sample_index(snapshots_qs, num_samples=5):
    """Look at the first `num_samples` CPU snapshots of each bot (ordered by time)
    and, for each sample position, report the max/p99/p95 of that position's value
    across every bot.

    This shows whether early-life samples run hotter than later ones: if sample 1's
    stats are higher than sample 5's, CPU tends to peak at the start of a bot's life.

    Done in a single windowed pass over the already-filtered snapshots. Returns a
    list of dicts (one per sample position) with keys sample_index, max, p99, p95,
    bot_count.
    """
    inner_qs = snapshots_qs.annotate(value=Cast("data__cpu_usage_millicores", FloatField())).values("bot_id", "created_at", "value").distinct()
    inner_sql, inner_params = inner_qs.query.sql_with_params()

    sql = f"""
        WITH filtered AS ({inner_sql}),
        ranked AS (
            SELECT
                value,
                ROW_NUMBER() OVER (PARTITION BY bot_id ORDER BY created_at ASC) AS sample_index
            FROM filtered
        )
        SELECT
            sample_index,
            MAX(value) AS max,
            PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY value) AS p99,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95,
            COUNT(*) AS bot_count
        FROM ranked
        WHERE sample_index <= %s
        GROUP BY sample_index
        ORDER BY sample_index
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, [*inner_params, num_samples])
        rows = cursor.fetchall()

    return [
        {
            "sample_index": row[0],
            "max": row[1],
            "p99": row[2],
            "p95": row[3],
            "bot_count": row[4],
        }
        for row in rows
    ]


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
    bot_count = snapshots_qs.values("bot_id").distinct().count()

    cpu_stats = _per_bot_stats(snapshots_qs, "cpu_usage_millicores")
    cpu_p99_stats = _per_bot_percentile_stats(snapshots_qs, "cpu_usage_millicores", 0.99)
    ram_stats = _per_bot_stats(snapshots_qs, "ram_usage_megabytes")
    top_bots_by_cpu_p99 = _top_bots_by_cpu_p99(snapshots_qs)
    cpu_by_sample = _cpu_by_sample_index(snapshots_qs)

    return {
        "window": window,
        "window_label": WINDOWS[window]["label"],
        "window_options": [{"key": key, "label": options["label"]} for key, options in WINDOWS.items()],
        "platform": platform,
        "platform_options": PLATFORM_OPTIONS,
        "recording": recording,
        "recording_options": RECORDING_OPTIONS,
        "snapshot_count": snapshots_qs.count(),
        "bot_count": bot_count,
        "latest_snapshot_at": latest_snapshot_at,
        "cpu_stats": cpu_stats,
        "cpu_p99_stats": cpu_p99_stats,
        "ram_stats": ram_stats,
        "top_bots_by_cpu_p99": top_bots_by_cpu_p99,
        "cpu_by_sample": cpu_by_sample,
    }
