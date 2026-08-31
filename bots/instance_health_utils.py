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

from django.conf import settings
from django.db.models import Avg, FloatField, Max, Q, Value
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Coalesce
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

# A backlog has to move by at least this much before it is called growing or draining
# rather than steady. Both bars have to be cleared: the percentage alone would make a
# queue that idles at one or two tasks look dramatic, and the absolute change alone
# would call a move from 400 to 410 a trend. The absolute floor also absorbs the case
# these averages are least suited to, an otherwise empty queue that took one burst.
QUEUE_TREND_MINIMUM_CHANGE = 2
QUEUE_TREND_MINIMUM_CHANGE_PERCENTAGE = 15

# Default window for deciding that a non-empty queue is not draining. The alert
# setting can override this; keeping the default in snapshot intervals makes the
# amount of evidence stable if the sampling cadence changes.
QUEUE_NOT_DRAINING_CONFIRMED_AFTER_SECONDS = INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS * 15

# How long every worker has to stay silent before the workers panel calls them gone
# rather than restarting. Worker stats are collected by the scheduler, which samples
# them as soon as it starts, so the first sample after a deploy is taken while the
# workers are themselves restarting and comes back empty. That reading is
# indistinguishable from a real outage on its own, so the panel keeps showing the last
# roster that replied until the silence has lasted this long. Two sampling intervals
# leaves room for a sample taken after the deploy has settled, while still surfacing a
# genuine outage within minutes.
WORKER_SILENCE_CONFIRMED_AFTER_SECONDS = INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS * 4


def celery_queue_has_not_decreased_for(threshold_seconds):
    """Whether any non-empty queue stayed flat or grew for the whole threshold."""
    latest_depths, latest_at = _latest_reading("celery_queue_depths")
    if not latest_depths or latest_at is None:
        return False

    cutoff = latest_at - timedelta(seconds=max(threshold_seconds, 0))
    queue_readings = InstanceHealthSnapshot.objects.filter(data__has_key="celery_queue_depths")

    # Include the last reading at or before the cutoff so the observations cover
    # the full configured range rather than merely beginning somewhere inside it.
    evidence = queue_readings.filter(created_at__lte=cutoff).order_by("-created_at").values_list("data", flat=True).first()
    if evidence is None:
        return False

    readings = [evidence]
    readings.extend(queue_readings.filter(created_at__gt=cutoff, created_at__lte=latest_at).order_by("created_at").values_list("data", flat=True))

    for queue_name, latest_depth in latest_depths.items():
        # A queue at zero is idle, not stalled. A missing value means Redis failed
        # to report that queue, so continuity cannot be established for this window.
        if latest_depth is None or latest_depth <= 0:
            continue

        depths = []
        for data in readings:
            queue_depths = data.get("celery_queue_depths") or {}
            if queue_name not in queue_depths or queue_depths[queue_name] is None:
                break
            depths.append(queue_depths[queue_name])
        else:
            if depths[0] > 0 and all(current >= previous for previous, current in zip(depths, depths[1:])):
                return True

    return False


def _worker_stats_have_alive_workers(worker_stats):
    """Whether a worker census contains an execution pool able to run tasks."""
    if not worker_stats:
        return False

    return any((worker or {}).get("processes_alive", 0) > 0 for worker in (worker_stats.get("workers") or {}).values())


def celery_workers_have_been_down_for(threshold_seconds):
    """Return whether every Celery execution pool has stayed down for the threshold.

    Time is measured between collected worker censuses, not from the latest census to
    wall-clock time. That distinction prevents a stopped scheduler (and therefore
    stale snapshots) from being misreported as a Celery outage.
    """
    latest_stats, latest_at = _latest_reading("celery_worker_stats")
    if latest_at is None or _worker_stats_have_alive_workers(latest_stats):
        return False

    cutoff = latest_at - timedelta(seconds=max(threshold_seconds, 0))
    worker_readings = InstanceHealthSnapshot.objects.filter(data__has_key="celery_worker_stats")

    # A down reading at or before the cutoff proves the outage spans the full
    # threshold. Without one, snapshot history does not go back far enough to know.
    evidence = worker_readings.filter(created_at__lte=cutoff).order_by("-created_at").values_list("data", flat=True).first()
    if evidence is None or _worker_stats_have_alive_workers(evidence.get("celery_worker_stats")):
        return False

    # Every census since the cutoff must still show no usable worker pool.
    subsequent_readings = worker_readings.filter(created_at__gt=cutoff, created_at__lte=latest_at).values_list("data", flat=True)
    return not any(_worker_stats_have_alive_workers(data.get("celery_worker_stats")) for data in subsequent_readings)


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


def _latest_worker_roster():
    """Return (worker stats, sampled_at) from the most recent snapshot a worker replied to.

    Snapshots that sampled worker stats and got no replies are skipped, so this is the
    last census that actually described running workers however far back that is.
    """
    row = InstanceHealthSnapshot.objects.annotate(replies=_json_number("celery_worker_stats", "worker_count")).filter(replies__gt=0).order_by("-created_at").values_list("data", "created_at").first()
    if row is None:
        return None, None

    data, created_at = row
    return data["celery_worker_stats"], created_at


def _peaks_over_window(cutoff, queue_names):
    """Return (peak connection count, [peak depth per queue]) since cutoff."""
    aggregates = {"connections": Max(_json_number("database_connections", "total"))}

    # Queue names come from settings and can contain characters that are not valid
    # keyword arguments, so the aggregates are aliased positionally.
    for index, queue_name in enumerate(queue_names):
        aggregates[f"queue_{index}"] = Max(_json_number("celery_queue_depths", queue_name))

    peaks = InstanceHealthSnapshot.objects.filter(created_at__gte=cutoff).aggregate(**aggregates)

    return _as_int(peaks["connections"]), [_as_int(peaks[f"queue_{index}"]) for index in range(len(queue_names))]


def _total_queue_depth_expression(queue_names):
    """Expression summing every queue's depth within a single snapshot.

    A queue that failed to be read is absent from the snapshot rather than recorded as
    zero, and is counted as zero here so that one unreadable queue does not null out
    the whole row's total.
    """
    terms = [Coalesce(_json_number("celery_queue_depths", queue_name), Value(0.0)) for queue_name in queue_names]

    total = terms[0]
    for term in terms[1:]:
        total = total + term

    return total


def _queue_trend_over_window(cutoff, now, queue_names):
    """Return how the backlog in the recent half of the window compares to the earlier half.

    Each half is averaged rather than compared end to end because a queue depth is a
    momentary reading: two individual snapshots can differ wildly while the backlog is
    flat, so a first-versus-last comparison would report a trend out of noise.

    Snapshots missing the metric entirely are excluded instead of being read as an
    empty set of queues, which would otherwise pull a half's average toward zero.
    """
    if not queue_names:
        return None

    midpoint = cutoff + (now - cutoff) / 2
    total_depth = _total_queue_depth_expression(queue_names)
    was_collected = Q(data__has_key="celery_queue_depths")

    averages = InstanceHealthSnapshot.objects.filter(created_at__gte=cutoff).aggregate(
        earlier=Avg(total_depth, filter=was_collected & Q(created_at__lt=midpoint)),
        later=Avg(total_depth, filter=was_collected & Q(created_at__gte=midpoint)),
    )

    earlier, later = averages["earlier"], averages["later"]

    # Either half is empty on an instance that only started snapshotting partway
    # through the window, which leaves nothing to compare against.
    if earlier is None or later is None:
        return None

    change = later - earlier
    change_percentage = abs(change) / earlier * 100 if earlier else None

    if abs(change) < QUEUE_TREND_MINIMUM_CHANGE or (change_percentage is not None and change_percentage < QUEUE_TREND_MINIMUM_CHANGE_PERCENTAGE):
        direction = {"label": "Steady", "status": "secondary", "icon": "bi-arrow-right"}
    elif change > 0:
        direction = {"label": "Growing", "status": "warning", "icon": "bi-arrow-up-right"}
    else:
        direction = {"label": "Draining", "status": "success", "icon": "bi-arrow-down-right"}

    return {
        "earlier_average": round(earlier, 1),
        "later_average": round(later, 1),
        **direction,
    }


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


def _build_queues(queue_names, queue_depths, queue_peaks, trend, sampled_at):
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
        "trend": trend,
        "sampled_at": sampled_at,
        "interval_label": _sampling_interval_label(INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS),
    }


def _build_workers(worker_stats, sampled_at, now):
    """Build the workers panel from the latest sample, falling back to the last one with replies.

    Workers going quiet for a sample or two is what a deploy looks like, so the latest
    sample coming back empty is not on its own enough to report that there are no
    workers: see WORKER_SILENCE_CONFIRMED_AFTER_SECONDS. Within that grace period the
    panel shows the last roster that replied instead, dated to when it was taken, and
    flags that the current sample has nothing in it.
    """
    if not worker_stats:
        return None

    replies_are_missing = not worker_stats.get("workers")

    if replies_are_missing:
        roster, roster_at = _latest_worker_roster()
        if roster is not None and (now - roster_at).total_seconds() <= WORKER_SILENCE_CONFIRMED_AFTER_SECONDS:
            worker_stats, sampled_at = roster, roster_at

    workers = worker_stats.get("workers") or {}
    rows = [{"name": worker_name, **(stats or {})} for worker_name, stats in sorted(workers.items())]

    return {
        # A worker that did not reply in time is indistinguishable from one that is
        # down, so this is a lower bound rather than an exact census.
        "worker_count": worker_stats.get("worker_count"),
        "total_concurrency": sum(row["concurrency"] for row in rows if row.get("concurrency")),
        "rows": rows,
        "replies_are_missing": replies_are_missing,
        "silence_grace_label": _interval_label(WORKER_SILENCE_CONFIRMED_AFTER_SECONDS),
        "sampled_at": sampled_at,
        "interval_label": _sampling_interval_label(INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS),
    }


def _table_sizes_baseline(cutoff, sampled_at):
    """Return (table sizes, sampled_at) for the reading current sizes are compared against.

    Growth is measured from the reading that was in force at the start of the window,
    which is the last one taken before the cutoff rather than the first one taken
    after it. Table sizes are sampled on an interval long enough that the narrower
    windows often hold no second reading to compare against, and comparing the
    earliest and latest readings inside the window would in any case understate the
    growth by up to a whole sampling interval.

    An instance whose readings all fall inside the window has no reading from before
    the cutoff and falls back to its earliest one, which spans less than the window.
    The panel dates the comparison from the returned timestamp rather than from the
    window, so either way it reports the span it actually measured. A single reading
    ever leaves nothing to compare against.
    """
    if sampled_at is None:
        return None, None

    earlier_readings = InstanceHealthSnapshot.objects.filter(data__has_key="database_table_sizes", created_at__lt=sampled_at).values_list("data", "created_at")
    row = earlier_readings.filter(created_at__lt=cutoff).order_by("-created_at").first() or earlier_readings.order_by("created_at").first()
    if row is None:
        return None, None

    data, created_at = row
    return data["database_table_sizes"], created_at


def _percentage_change(change, baseline_value):
    """Percentage form of an absolute change, or None where there is no base to divide by.

    A table that grew from nothing has no percentage: it is reported as new instead.
    """
    return round(change / baseline_value * 100, 1) if baseline_value else None


def _table_size_row(table_name, size, total_bytes, baseline_tables):
    """One row of the largest tables panel, with growth since the baseline reading.

    baseline_tables is None when there is nothing to compare against, which leaves the
    growth unreported rather than reported as zero. A table absent from a baseline that
    does exist was created within the window, so all of its size is growth.
    """
    has_baseline = baseline_tables is not None
    baseline_size = baseline_tables.get(table_name) if has_baseline else None

    return {
        "name": table_name,
        "bytes": size,
        "percentage": round(size / total_bytes * 100, 1) if total_bytes else 0,
        "growth_bytes": size - (baseline_size or 0) if has_baseline else None,
        "growth_percentage": _percentage_change(size - baseline_size, baseline_size) if baseline_size is not None else None,
        "is_new": has_baseline and baseline_size is None,
    }


def _build_table_sizes(table_sizes, sampled_at, baseline, baseline_at):
    if not table_sizes:
        return None

    tables = table_sizes.get("tables") or {}
    total_bytes = table_sizes.get("total_bytes") or 0

    baseline_tables = (baseline.get("tables") or {}) if baseline else None
    baseline_total_bytes = (baseline.get("total_bytes") or 0) if baseline else None

    # Snapshots already store these largest first, but re-sorting here keeps the panel
    # from depending on that ordering surviving the JSON round trip.
    largest = sorted(tables.items(), key=lambda item: item[1], reverse=True)[:TABLE_SIZE_LIMIT]

    return {
        "total_bytes": total_bytes,
        "table_count": len(tables),
        "rows": [_table_size_row(table_name, size, total_bytes, baseline_tables) for table_name, size in largest],
        # The span the growth figures cover, which the panel reports in place of the
        # selected window because the baseline reading rarely lands on the cutoff.
        "growth_since": baseline_at if baseline else None,
        "growth_bytes": total_bytes - baseline_total_bytes if baseline else None,
        "growth_percentage": _percentage_change(total_bytes - baseline_total_bytes, baseline_total_bytes) if baseline else None,
        "sampled_at": sampled_at,
        "interval_label": _sampling_interval_label(INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS),
    }


def user_can_view_instance_health(user):
    """Whether the instance health dashboard exists for this user.

    There is nothing to show unless snapshots are being written, and deployments can
    narrow the dashboard to superusers even though the rest of the project pages are
    open to any admin.
    """
    if not settings.SAVE_INSTANCE_HEALTH_SNAPSHOTS:
        return False

    return user.is_superuser or not settings.INSTANCE_HEALTH_ONLY_VIEWABLE_BY_SUPERUSERS


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
    queue_trend = _queue_trend_over_window(cutoff, now, queue_names)
    table_sizes_baseline, table_sizes_baseline_at = _table_sizes_baseline(cutoff, table_sizes_at)

    return {
        "window": window,
        "window_label": WINDOWS[window]["label"],
        "window_options": [{"key": key, "label": options["label"]} for key, options in WINDOWS.items()],
        "snapshot_count": InstanceHealthSnapshot.objects.filter(created_at__gte=cutoff).count(),
        "snapshot_interval_label": _interval_label(INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS),
        "latest_snapshot_at": latest_snapshot_at,
        "snapshots_are_stale": latest_snapshot_at is not None and (now - latest_snapshot_at).total_seconds() > STALE_AFTER_SECONDS,
        "connections": _build_connections(connections, connections_peak, connections_at),
        "queues": _build_queues(queue_names, queue_depths, queue_peaks, queue_trend, queue_depths_at),
        "workers": _build_workers(worker_stats, worker_stats_at, now),
        "table_sizes": _build_table_sizes(table_sizes, table_sizes_at, table_sizes_baseline, table_sizes_baseline_at),
    }
