"""
Read side of the instance health snapshots written by bots.instance_health_snapshot_taker.

Two properties of how snapshots are written drive everything here:

  * Any metric can be missing from any given snapshot, either because its collector
    failed that cycle or because it is sampled on a longer interval than the snapshot
    cadence (table sizes, worker stats). So the current value of a metric comes from
    the most recent snapshot that actually carries it, which is not necessarily the
    most recent snapshot, and every panel reports its own cadence and the age of its
    own reading.
  * A window holds one row per INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS, which is
    thousands of rows at the wider settings. Peaks are therefore aggregated in
    Postgres; the only rows pulled into Python are the handful backing the current
    values.
"""

import math
from datetime import timedelta

from django.db.models import FloatField, Max
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast
from django.utils import timezone

from .instance_health_snapshot_taker import (
    INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS,
    INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS,
    INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS,
)
from .models import InstanceHealthSnapshot

# Windows the dashboard offers, kept inside the default retention window so a
# selection can never come back empty just because the rows were already trimmed.
WINDOWS = {
    "1h": {"label": "1 hour", "hours": 1},
    "6h": {"label": "6 hours", "hours": 6},
    "24h": {"label": "24 hours", "hours": 24},
    "7d": {"label": "7 days", "hours": 24 * 7},
}
DEFAULT_WINDOW = "24h"

# Enough of the largest tables to show what is driving database size without turning
# the panel into a full catalog listing.
TABLE_SIZE_LIMIT = 15

# A snapshot is due every INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS, so a latest
# snapshot much older than that means the scheduler stopped reporting and every
# current value on the dashboard is describing the past.
STALE_AFTER_SECONDS = INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS * 3

# Thresholds for colouring connection usage. The usable ceiling sits somewhat below
# max_connections, since superuser slots are reserved and managed hosts run their own
# internal sessions, which is what makes 85% rather than 100% the danger line.
CONNECTION_WARNING_PERCENTAGE = 70
CONNECTION_DANGER_PERCENTAGE = 85


def _interval_label(seconds):
    """Render a sampling interval the way an operator would say it: 30s, 10m, 1h."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _sampling_interval_label(metric_interval_seconds):
    """Label for how often a metric is really collected.

    A metric on a longer interval is still only collected when a snapshot is due, so
    its real cadence is its own interval rounded up to a whole number of snapshots.
    """
    snapshots_per_sample = max(math.ceil(metric_interval_seconds / INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS), 1)

    return _interval_label(snapshots_per_sample * INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS)


def _latest_reading(metric_key):
    """Return (value, sampled_at) from the most recent snapshot carrying metric_key.

    Ordering by created_at lets Postgres walk the index and stop at the first snapshot
    that has the key, so this stays cheap even for metrics that appear on only a small
    fraction of snapshots.
    """
    row = InstanceHealthSnapshot.objects.filter(data__has_key=metric_key).order_by("-created_at").values_list("data", "created_at").first()
    if row is None:
        return None, None

    data, created_at = row
    return data.get(metric_key), created_at


def _json_number(*path):
    """Expression reading the number at path within a snapshot's JSON data.

    Intermediate keys are traversed as JSON and only the leaf is pulled out as text,
    since text is what Cast needs and what a nested key lookup cannot be applied to.

    Cast to float rather than integer because Postgres rejects a value like "12.5" as
    an integer, and one unexpected non-integral sample would fail the whole aggregate.
    """
    *containers, leaf = path

    expression = "data"
    for key in containers:
        expression = KeyTransform(key, expression)

    return Cast(KeyTextTransform(leaf, expression), FloatField())


def _as_int(value):
    return None if value is None else int(value)


def _peaks_over_window(cutoff, queue_names):
    """Return (peak connection count, [peak depth per queue]) since cutoff."""
    aggregates = {"connections": Max(_json_number("database_connections", "total"))}

    # Queue names come from settings and can contain characters that are not valid
    # keyword arguments, so the aggregates are aliased positionally.
    for index, queue_name in enumerate(queue_names):
        aggregates[f"queue_{index}"] = Max(_json_number("celery_queue_depths", queue_name))

    peaks = InstanceHealthSnapshot.objects.filter(created_at__gte=cutoff).aggregate(**aggregates)

    return _as_int(peaks["connections"]), [_as_int(peaks[f"queue_{index}"]) for index in range(len(queue_names))]


def _build_connections(connections, peak, sampled_at):
    if not connections:
        return None

    max_connections = connections.get("max_connections")
    used_percentage = connections.get("used_percentage")

    if used_percentage is None or used_percentage < CONNECTION_WARNING_PERCENTAGE:
        status = "success"
    elif used_percentage < CONNECTION_DANGER_PERCENTAGE:
        status = "warning"
    else:
        status = "danger"

    return {
        "used": connections.get("total"),
        "max_connections": max_connections,
        "used_percentage": used_percentage,
        "peak": peak,
        "peak_percentage": round(peak / max_connections * 100, 1) if peak is not None and max_connections else None,
        "status": status,
        "sampled_at": sampled_at,
        "interval_label": _sampling_interval_label(INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS),
    }


def _build_queues(queue_names, queue_depths, queue_peaks, sampled_at):
    depths = queue_depths or {}
    rows = [
        {
            "name": queue_name,
            "depth": depths.get(queue_name),
            "peak": peak,
        }
        for queue_name, peak in zip(queue_names, queue_peaks)
    ]

    return {
        "rows": rows,
        "total_depth": sum(depth for depth in depths.values() if depth is not None),
        "sampled_at": sampled_at,
        "interval_label": _sampling_interval_label(INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS),
    }


def _build_workers(worker_stats, sampled_at):
    if not worker_stats:
        return None

    workers = worker_stats.get("workers") or {}
    rows = [{"name": worker_name, **(stats or {})} for worker_name, stats in sorted(workers.items())]

    return {
        # A worker that did not reply in time is indistinguishable from one that is
        # down, so this is a lower bound rather than an exact census.
        "worker_count": worker_stats.get("worker_count"),
        "total_concurrency": sum(row["concurrency"] for row in rows if row.get("concurrency")),
        "rows": rows,
        "sampled_at": sampled_at,
        "interval_label": _sampling_interval_label(INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS),
    }


def _build_table_sizes(table_sizes, sampled_at):
    if not table_sizes:
        return None

    tables = table_sizes.get("tables") or {}
    total_bytes = table_sizes.get("total_bytes") or 0

    # Snapshots already store these largest first, but re-sorting here keeps the panel
    # from depending on that ordering surviving the JSON round trip.
    largest = sorted(tables.items(), key=lambda item: item[1], reverse=True)[:TABLE_SIZE_LIMIT]

    return {
        "total_bytes": total_bytes,
        "table_count": len(tables),
        "rows": [
            {
                "name": table_name,
                "bytes": size,
                "percentage": round(size / total_bytes * 100, 1) if total_bytes else 0,
            }
            for table_name, size in largest
        ],
        "sampled_at": sampled_at,
        "interval_label": _sampling_interval_label(INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS),
    }


def get_instance_health_data(window=DEFAULT_WINDOW):
    """Return the template context for the instance health dashboard.

    Current values are whatever each metric last reported; peaks and the snapshot
    count cover the selected window.
    """
    if window not in WINDOWS:
        window = DEFAULT_WINDOW

    now = timezone.now()
    cutoff = now - timedelta(hours=WINDOWS[window]["hours"])

    latest_snapshot_at = InstanceHealthSnapshot.objects.order_by("-created_at").values_list("created_at", flat=True).first()

    connections, connections_at = _latest_reading("database_connections")
    queue_depths, queue_depths_at = _latest_reading("celery_queue_depths")
    worker_stats, worker_stats_at = _latest_reading("celery_worker_stats")
    table_sizes, table_sizes_at = _latest_reading("database_table_sizes")

    # A queue that could not be read is omitted from the snapshot rather than recorded
    # as zero, so the set of queues is taken from the latest reading of it.
    queue_names = sorted(queue_depths or {})
    connections_peak, queue_peaks = _peaks_over_window(cutoff, queue_names)

    return {
        "window": window,
        "window_label": WINDOWS[window]["label"],
        "window_options": [{"key": key, "label": options["label"]} for key, options in WINDOWS.items()],
        "snapshot_count": InstanceHealthSnapshot.objects.filter(created_at__gte=cutoff).count(),
        "snapshot_interval_label": _interval_label(INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS),
        "latest_snapshot_at": latest_snapshot_at,
        "snapshots_are_stale": latest_snapshot_at is not None and (now - latest_snapshot_at).total_seconds() > STALE_AFTER_SECONDS,
        "connections": _build_connections(connections, connections_peak, connections_at),
        "queues": _build_queues(queue_names, queue_depths, queue_peaks, queue_depths_at),
        "workers": _build_workers(worker_stats, worker_stats_at),
        "table_sizes": _build_table_sizes(table_sizes, table_sizes_at),
    }
