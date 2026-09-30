"""
Instance-wide health snapshots: Celery queue depths, database connection usage and
per-table sizes.

These are collected from the scheduler loop rather than from a Celery task on
purpose. Queue backlog and connection exhaustion are exactly the conditions under
which a worker-based collector stops reporting, so the metric would disappear at
the moment it becomes interesting. The scheduler is a plain loop with no worker
dependency, which makes it the right observer.

Cost notes, since the caller polls this on every cycle:

  * Queue depth is a Redis LLEN, which is O(1).
  * The connection query scans a fixed-size shared-memory array sized to
    max_connections, not a table. Roughly a millisecond.
  * Table sizes are the only non-trivial one. pg_total_relation_size() takes no
    locks and scans nothing, but it does a stat() per 1 GB file segment per fork,
    across each table, its TOAST relation and every index. That is usually tens of
    milliseconds, but can spike on a cold dentry cache or network storage, which is
    why it is sampled on a longer interval.
  * Celery worker stats cost a flat second, because inspect() blocks for its whole
    timeout rather than returning once every worker has replied. Also sampled on a
    longer interval, since pool sizing only changes on deploys and restarts.
  * Bot fatal error stats walk the bot events primary key index backwards and stop
    after a fixed number of rows, so the cost is set by the sample size rather than
    by how large the bot events table has grown. bots_botevent has no index on
    created_at, so the time window is applied to that sample rather than used to
    find the rows.

The scheduler also trims this table to a fixed retention window as it goes, so the
snapshots stay bounded without depending on a separate cleanup job.
"""

import logging
import os
import time
from datetime import timedelta

import redis
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from attendee.celery import app as celery_app
from bots.models import BotEvent, BotEventTypes, BotStates, InstanceHealthSnapshot

logger = logging.getLogger(__name__)

# How often to write a snapshot. Throttled inside the taker so the metric cadence
# is independent of how often the caller polls.
INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS", "60"))

# Table sizes move on the scale of hours and cost more to measure than the other
# metrics, so they are sampled on a longer interval and attached to whichever
# snapshot happens to be due when they are collected.
INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS = int(os.getenv("INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS", "3600"))

# How far back snapshots are kept. Anything older is deleted, which is what stops
# this table from growing without bound at one row per
# INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS.
INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS = int(os.getenv("INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS", str(14 * 24 * 60 * 60)))

# How often the retention window is enforced. Far longer than the snapshot interval
# because a little overshoot past the window costs nothing.
INSTANCE_HEALTH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS = int(os.getenv("INSTANCE_HEALTH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS", "3600"))

# Deletion is bounded per pass so it cannot stall the caller's loop: at steady state
# a pass has only INSTANCE_HEALTH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS worth of rows to
# remove, and a large backlog (a retention window that was just shortened, or a table
# that predates this cleanup) is drained over several passes instead of one long
# transaction.
INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE = int(os.getenv("INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE", "500"))
INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS = int(os.getenv("INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS", "10"))

# These queries run against a database that may already be struggling, which is
# precisely when the numbers matter. A short timeout keeps a slow database from
# stalling the caller's loop.
INSTANCE_HEALTH_METRIC_STATEMENT_TIMEOUT_MS = int(os.getenv("INSTANCE_HEALTH_METRIC_STATEMENT_TIMEOUT_MS", "2000"))

# inspect() blocks for the full timeout collecting replies, so this is a fixed
# cost added to every snapshot that samples worker stats. Keep it short.
INSTANCE_HEALTH_CELERY_WORKER_STATS_TIMEOUT_SECONDS = float(os.getenv("INSTANCE_HEALTH_CELERY_WORKER_STATS_TIMEOUT_SECONDS", "1.0"))

# Pool sizing changes only when workers are deployed or restarted, so it is sampled
# on a longer interval than the other metrics and attached to whichever snapshot
# happens to be due when it is collected.
INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS = int(os.getenv("INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS", "300"))

# How far back bot fatal errors are counted. Defaults to 30 minutes.
INSTANCE_HEALTH_BOT_FATAL_ERROR_WINDOW_SECONDS = int(os.getenv("INSTANCE_HEALTH_BOT_FATAL_ERROR_WINDOW_SECONDS", "1800"))

# How many of the most recent bot events are read to find the ones inside the window.
# This caps the cost of the query. When more events than this land inside the window,
# only the most recent ones are counted, and the snapshot records that the sample was
# full so the counts can be read as a lower bound. Defaults to 5000.
INSTANCE_HEALTH_BOT_EVENTS_SAMPLE_SIZE = int(os.getenv("INSTANCE_HEALTH_BOT_EVENTS_SAMPLE_SIZE", "5000"))

# Background workers (autovacuum, walwriter, ...) appear in pg_stat_activity but do
# not consume a max_connections slot, so they are excluded to keep the ratio honest.
CONNECTION_STATS_SQL = """
    SELECT
        count(*),
        current_setting('max_connections')::int
    FROM pg_stat_activity
    WHERE backend_type = 'client backend'
"""

# relkind 'r' covers every relation that actually holds storage, including
# individual partitions. Partitioned parents are excluded because they hold none.
TABLE_SIZES_SQL = """
    SELECT n.nspname, c.relname, pg_total_relation_size(c.oid)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
"""


# The inner query must order by id: that is what lets Postgres read the primary key
# index backwards and stop at the LIMIT. Ordering by created_at, which has no index,
# would sort the whole table instead.
#
# A bot that finished is one whose event moved it into FATAL_ERROR or ENDED. That
# includes COULD_NOT_JOIN, which also lands in FATAL_ERROR but is not itself a fatal
# error event, so it counts toward the total without counting as a failure.
#
# Built at call time rather than import time so the settings it reads can be patched.
def bot_fatal_error_stats_sql():
    return f"""
        SELECT
            count(DISTINCT bot_id) FILTER (WHERE event_type = {int(BotEventTypes.FATAL_ERROR)}),
            count(DISTINCT bot_id) FILTER (WHERE new_state IN ({int(BotStates.FATAL_ERROR)}, {int(BotStates.ENDED)})),
            count(*)
        FROM (
            SELECT bot_id, event_type, new_state, created_at
            FROM {BotEvent._meta.db_table}
            ORDER BY id DESC
            LIMIT {int(INSTANCE_HEALTH_BOT_EVENTS_SAMPLE_SIZE)}
        ) AS recent_events
        WHERE created_at >= now() - make_interval(secs => {int(INSTANCE_HEALTH_BOT_FATAL_ERROR_WINDOW_SECONDS)})
    """


def _fetch_with_timeout(sql):
    """Run a read-only metric query under a transaction-local statement timeout.

    set_config() is used rather than SET LOCAL because SET does not accept bind
    parameters over the extended query protocol.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(INSTANCE_HEALTH_METRIC_STATEMENT_TIMEOUT_MS)])
            cursor.execute(sql)
            return cursor.fetchall()


def get_celery_worker_stats():
    """Return per-worker pool concurrency as reported by the workers themselves.

    A worker that fails to reply within the timeout is indistinguishable from a
    worker that is down, so worker_count is a lower bound, not an exact census.
    """

    replies = celery_app.control.inspect(timeout=INSTANCE_HEALTH_CELERY_WORKER_STATS_TIMEOUT_SECONDS).stats() or {}

    workers = {}
    for worker_name, stats in replies.items():
        pool = stats.get("pool") or {}
        workers[worker_name] = {
            "concurrency": pool.get("max-concurrency"),
            "processes_alive": len(pool.get("processes") or []),
            "prefetch_count": stats.get("prefetch_count"),
        }

    return {"worker_count": len(workers), "workers": workers}


def get_celery_queue_depths(redis_client):
    """Return {queue_name: depth} for every queue named in CELERY_TASK_ROUTES.

    A queue that cannot be read is omitted rather than reported as zero, so a Redis
    hiccup is never mistaken for a drained queue.
    """
    queue_names = sorted({"celery"} | {route.get("queue", "celery") for route in settings.CELERY_TASK_ROUTES.values()})

    depths = {}
    for queue_name in queue_names:
        try:
            depths[queue_name] = redis_client.llen(queue_name)
        except Exception as e:
            logger.error(f"Error getting depth of Celery queue {queue_name}: {e}. Continuing...")
    return depths


def get_database_connection_stats():
    """Return how many connection slots are in use versus the max_connections ceiling.

    Only the row count is read from pg_stat_activity, never per-row detail columns,
    so this works for unprivileged users on managed hosts (RDS, Cloud SQL, Azure)
    where columns like state are masked for other users' sessions.

    Note the practical ceiling is slightly below max_connections: superuser slots
    are reserved and managed hosts run internal sessions. Alerting around 80-85%
    accounts for that without needing to model it.
    """
    total, max_connections = _fetch_with_timeout(CONNECTION_STATS_SQL)[0]

    return {
        "total": total,
        "max_connections": max_connections,
        "used_percentage": round(total / max_connections * 100, 2) if max_connections else None,
    }


def get_database_table_sizes():
    """Return per-table total size in bytes, keyed by "schema.table", plus the sum.

    Sizes include each table's TOAST relation and all of its indexes.
    """
    rows = _fetch_with_timeout(TABLE_SIZES_SQL)

    tables = {f"{schema}.{table}": size for schema, table, size in rows if size is not None}
    return {
        "total_bytes": sum(tables.values()),
        "tables": dict(sorted(tables.items(), key=lambda item: item[1], reverse=True)),
    }


def get_bot_fatal_error_stats():
    """Return how many bots hit a fatal error in the window, and what share of finished bots that is.

    Only the most recent INSTANCE_HEALTH_BOT_EVENTS_SAMPLE_SIZE events are looked at.
    sample_is_full means every one of them fell inside the window, so older events
    in the window may have been missed and the counts are a lower bound. The
    percentage is still a fair estimate in that case, since it is taken over the
    most recent part of the window.
    """
    fatal_error_bot_count, finished_bot_count, events_in_window = _fetch_with_timeout(bot_fatal_error_stats_sql())[0]

    return {
        "window_seconds": INSTANCE_HEALTH_BOT_FATAL_ERROR_WINDOW_SECONDS,
        "fatal_error_bot_count": fatal_error_bot_count,
        "finished_bot_count": finished_bot_count,
        "fatal_error_percentage": round(fatal_error_bot_count / finished_bot_count * 100, 2) if finished_bot_count else None,
        "sample_is_full": events_in_window >= INSTANCE_HEALTH_BOT_EVENTS_SAMPLE_SIZE,
    }


def delete_snapshots_outside_retention_window():
    """Delete snapshots older than INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS, oldest first.

    Deletes at most INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE *
    INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS rows and returns how many
    were removed. Rows are selected by id so each batch is a short, constant-size
    transaction regardless of how far behind the table is.
    """
    cutoff = timezone.now() - timedelta(seconds=INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS)

    total_deleted = 0
    for _batch in range(INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS):
        ids = list(InstanceHealthSnapshot.objects.filter(created_at__lt=cutoff).order_by("id").values_list("id", flat=True)[:INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE])
        if not ids:
            break

        deleted, _ = InstanceHealthSnapshot.objects.filter(id__in=ids).delete()
        total_deleted += deleted

    return total_deleted


class InstanceHealthSnapshotTaker:
    """
    A class to handle taking snapshots of instance-wide health (Celery queue depths,
    database connections, table sizes).

    It owns its own Redis client and its own sampling clocks, so the caller only has
    to poll save_snapshot_if_needed() as often as it likes.
    """

    def __init__(self):
        self._redis_client = None
        self._last_snapshot_time = None
        self._last_table_size_sample_time = None
        self._last_celery_worker_stats_sample_time = None
        self._last_cleanup_time = None

    def _get_redis_client(self):
        if self._redis_client is None:
            self._redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS, socket_timeout=2, socket_connect_timeout=2)
        return self._redis_client

    def save_snapshot_if_needed(self):
        """Take a snapshot if one is due, otherwise return immediately.

        Every metric is gathered independently so that a failure to read one still
        records the others; a partial snapshot is more useful than none, and these
        numbers matter most when something is already broken.
        """
        # Only take snapshot if the env var is set to true
        if not settings.SAVE_INSTANCE_HEALTH_SNAPSHOTS:
            return

        # Monotonic rather than wall clock, since these are pure interval throttles.
        now = time.monotonic()

        if self._last_snapshot_time is not None and (now - self._last_snapshot_time) < INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS:
            return

        # Recorded up front so a failing collector can't turn into a retry loop.
        self._last_snapshot_time = now

        # Before collecting, so that the table is still trimmed on an instance where
        # every metric happens to be failing.
        self._delete_old_snapshots_if_needed(now)

        snapshot_data = {}

        try:
            snapshot_data["celery_queue_depths"] = get_celery_queue_depths(self._get_redis_client())
        except Exception as e:
            logger.error(f"Error getting Celery queue depths: {e}. Continuing...")
            self._redis_client = None  # Reset connection on failure

        if self._last_celery_worker_stats_sample_time is None or (now - self._last_celery_worker_stats_sample_time) >= INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS:
            # Recorded up front so a failing collector can't turn into a retry loop.
            self._last_celery_worker_stats_sample_time = now
            try:
                snapshot_data["celery_worker_stats"] = get_celery_worker_stats()
            except Exception as e:
                logger.error(f"Error getting Celery worker stats: {e}. Continuing...")

        try:
            snapshot_data["database_connections"] = get_database_connection_stats()
        except Exception as e:
            logger.error(f"Error getting database connection stats: {e}. Continuing...")

        try:
            snapshot_data["bot_fatal_errors"] = get_bot_fatal_error_stats()
        except Exception as e:
            logger.error(f"Error getting bot fatal error stats: {e}. Continuing...")

        if self._last_table_size_sample_time is None or (now - self._last_table_size_sample_time) >= INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS:
            # Recorded up front so a failing collector can't turn into a retry loop.
            self._last_table_size_sample_time = now
            try:
                snapshot_data["database_table_sizes"] = get_database_table_sizes()
            except Exception as e:
                logger.error(f"Error getting database table sizes: {e}. Continuing...")

        if not snapshot_data:
            logger.error("Every instance health metric failed to collect, skipping snapshot")
            return

        try:
            InstanceHealthSnapshot.objects.create(data=snapshot_data)
        except Exception as e:
            logger.error(f"Error saving instance health snapshot: {e}. Continuing...")
            return

        elapsed_seconds = time.monotonic() - now
        logger.info(f"Saved instance health snapshot in {elapsed_seconds:.3f}s: {snapshot_data}", extra={"instance_health_snapshot_data": snapshot_data})

    def _delete_old_snapshots_if_needed(self, now):
        """Enforce the retention window if a cleanup pass is due."""
        if self._last_cleanup_time is not None and (now - self._last_cleanup_time) < INSTANCE_HEALTH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS:
            return

        # Recorded up front so a failing delete can't turn into a retry loop.
        self._last_cleanup_time = now

        try:
            deleted = delete_snapshots_outside_retention_window()
        except Exception as e:
            logger.error(f"Error deleting instance health snapshots outside the retention window: {e}. Continuing...")
            return

        if deleted:
            logger.info(f"Deleted {deleted} instance health snapshots older than {INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS} seconds")
