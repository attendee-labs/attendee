"""Tests for bots.instance_health_snapshot_taker.

The database collectors are exercised against the real test database rather than a
mocked cursor, because half of what matters about them is what they cost. They are
called from the scheduler loop, so a collector that becomes expensive delays every
other thing the scheduler does, and a mocked cursor would happily keep passing.

The timing assertions carry a lot of headroom over what these queries actually cost
(single digit milliseconds), so they fail on a regression in kind, such as a query
issued per table or a lost index, rather than on a busy CI runner. Where a bound can
be expressed as a query count instead of a wall clock, it is, since that is exact.
"""

import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.db import connection
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bots.instance_health_snapshot_taker import (
    CONNECTION_STATS_SQL,
    INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS,
    INSTANCE_HEALTH_CELERY_WORKER_STATS_TIMEOUT_SECONDS,
    INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE,
    INSTANCE_HEALTH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS,
    INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS,
    INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS,
    INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS,
    INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS,
    InstanceHealthSnapshotTaker,
    _fetch_with_timeout,
    delete_snapshots_outside_retention_window,
    get_celery_queue_depths,
    get_celery_worker_stats,
    get_database_connection_stats,
    get_database_table_sizes,
)
from bots.models import InstanceHealthSnapshot

# Doubles as the patch target for the module's own globals and as the name of the
# logger it reports collector failures on.
MODULE = "bots.instance_health_snapshot_taker"

SNAPSHOT_TABLE = InstanceHealthSnapshot._meta.db_table

# A scan of the fixed size pg_stat_activity array, which is roughly a millisecond.
CONNECTION_STATS_BUDGET_SECONDS = 0.25

# A stat() per file segment per fork across every table, TOAST relation and index,
# which is tens of milliseconds over the test database's few hundred relations.
TABLE_SIZES_BUDGET_SECONDS = 0.5

# A worst case pass: a backlog deep enough that every batch is full and the per-pass
# cap is what ends it. Steady state is a couple of batches at most.
CLEANUP_PASS_BUDGET_SECONDS = 5.0

# Everything a due snapshot does apart from Redis and Celery, which are stood in for
# here because neither is a database cost: cleanup, both metric queries, the insert.
FULL_SNAPSHOT_BUDGET_SECONDS = 1.0


def _create_snapshot(data=None, created_at=None):
    """Create one snapshot, optionally backdated.

    created_at is auto_now_add, which ignores assignment at create time, so it has to
    be set with an update() afterwards.
    """
    snapshot = InstanceHealthSnapshot.objects.create(data=data or {})
    if created_at is not None:
        InstanceHealthSnapshot.objects.filter(pk=snapshot.pk).update(created_at=created_at)
        snapshot.refresh_from_db()
    return snapshot


def _create_snapshots(count, created_at):
    """Create count snapshots sharing one created_at, cheaply enough to build a backlog."""
    snapshots = InstanceHealthSnapshot.objects.bulk_create([InstanceHealthSnapshot(data={}) for _ in range(count)], batch_size=1000)
    InstanceHealthSnapshot.objects.filter(pk__gte=snapshots[0].pk, pk__lte=snapshots[-1].pk).update(created_at=created_at)
    return snapshots


def _statements_touching_snapshots(captured_queries):
    """The captured statements against the snapshot table, dropping savepoint noise."""
    return [query["sql"] for query in captured_queries if SNAPSHOT_TABLE in query["sql"]]


def _statements_excluding_savepoints(captured_queries):
    return [query["sql"] for query in captured_queries if "SAVEPOINT" not in query["sql"]]


class TimingAssertionsMixin:
    def assertFasterThan(self, budget_seconds, operation, runs=5):
        """Assert the fastest of several runs of operation comes in under the budget.

        The fastest run is used rather than the average because scheduling noise on a
        shared runner can only ever add time, which makes the floor the closest thing
        available to a measurement of the operation itself.
        """
        fastest = None
        for _ in range(runs):
            started = time.perf_counter()
            operation()
            elapsed = time.perf_counter() - started
            fastest = elapsed if fastest is None else min(fastest, elapsed)

        self.assertLess(fastest, budget_seconds, f"Fastest of {runs} runs took {fastest * 1000:.1f}ms, budget is {budget_seconds * 1000:.0f}ms")


class GetCeleryQueueDepthsTestCase(TestCase):
    def setUp(self):
        self.depths_by_queue = {"celery": 1, "bots": 2, "webhooks": 3}
        self.redis_client = MagicMock()
        self.redis_client.llen.side_effect = lambda queue_name: self.depths_by_queue[queue_name]

    @override_settings(CELERY_TASK_ROUTES={"task.a": {"queue": "bots"}, "task.b": {"queue": "webhooks"}})
    def test_reads_every_queue_named_in_the_task_routes(self):
        self.assertEqual(get_celery_queue_depths(self.redis_client), {"bots": 2, "celery": 1, "webhooks": 3})

    @override_settings(CELERY_TASK_ROUTES={})
    def test_reads_the_default_queue_even_when_nothing_is_routed_anywhere(self):
        self.assertEqual(get_celery_queue_depths(self.redis_client), {"celery": 1})

    @override_settings(CELERY_TASK_ROUTES={"task.a": {}, "task.b": {"queue": "bots"}})
    def test_a_route_without_a_queue_falls_back_to_the_default_queue(self):
        self.assertEqual(get_celery_queue_depths(self.redis_client), {"bots": 2, "celery": 1})

    @override_settings(CELERY_TASK_ROUTES={"task.a": {"queue": "bots"}, "task.b": {"queue": "bots"}})
    def test_a_queue_shared_by_several_routes_is_only_read_once(self):
        get_celery_queue_depths(self.redis_client)

        self.assertEqual(self.redis_client.llen.call_count, 2)

    @override_settings(CELERY_TASK_ROUTES={"task.a": {"queue": "bots"}})
    def test_an_unreadable_queue_is_omitted_rather_than_reported_as_drained(self):
        # Reporting zero would make a Redis hiccup indistinguishable from a queue
        # that had just been worked off, which is the opposite of what it means.
        def llen(queue_name):
            if queue_name == "bots":
                raise ConnectionError("redis is down")
            return self.depths_by_queue[queue_name]

        self.redis_client.llen.side_effect = llen

        with self.assertLogs(MODULE, level="ERROR"):
            depths = get_celery_queue_depths(self.redis_client)

        self.assertEqual(depths, {"celery": 1})


class GetCeleryWorkerStatsTestCase(TestCase):
    def _patch_inspect(self, replies):
        patcher = patch(f"{MODULE}.celery_app.control.inspect")
        mock_inspect = patcher.start()
        self.addCleanup(patcher.stop)
        mock_inspect.return_value.stats.return_value = replies
        return mock_inspect

    def test_summarises_the_pool_of_every_worker_that_replied(self):
        self._patch_inspect(
            {
                "celery@one": {"pool": {"max-concurrency": 4, "processes": [11, 12, 13, 14]}, "prefetch_count": 16},
                "celery@two": {"pool": {"max-concurrency": 2, "processes": [21]}, "prefetch_count": 8},
            }
        )

        self.assertEqual(
            get_celery_worker_stats(),
            {
                "worker_count": 2,
                "workers": {
                    "celery@one": {"concurrency": 4, "processes_alive": 4, "prefetch_count": 16},
                    "celery@two": {"concurrency": 2, "processes_alive": 1, "prefetch_count": 8},
                },
            },
        )

    def test_no_replies_at_all_is_reported_as_an_empty_census(self):
        # inspect() returns None rather than an empty mapping when nothing answers.
        self._patch_inspect(None)

        self.assertEqual(get_celery_worker_stats(), {"worker_count": 0, "workers": {}})

    def test_a_worker_that_reports_no_pool_section_is_still_counted(self):
        self._patch_inspect({"celery@one": {}})

        self.assertEqual(get_celery_worker_stats(), {"worker_count": 1, "workers": {"celery@one": {"concurrency": None, "processes_alive": 0, "prefetch_count": None}}})

    def test_inspect_is_bounded_by_the_configured_timeout(self):
        # inspect() blocks for the whole timeout rather than returning once every
        # worker has replied, so this is a flat cost on every snapshot that samples it.
        mock_inspect = self._patch_inspect({})

        get_celery_worker_stats()

        self.assertEqual(mock_inspect.call_args.kwargs["timeout"], INSTANCE_HEALTH_CELERY_WORKER_STATS_TIMEOUT_SECONDS)


class GetDatabaseConnectionStatsTestCase(TimingAssertionsMixin, TestCase):
    def test_reports_usage_against_the_max_connections_ceiling(self):
        stats = get_database_connection_stats()

        with connection.cursor() as cursor:
            cursor.execute("SHOW max_connections")
            max_connections = int(cursor.fetchone()[0])

        self.assertEqual(stats["max_connections"], max_connections)
        # This test is itself holding one of the connections being counted.
        self.assertGreaterEqual(stats["total"], 1)
        self.assertLessEqual(stats["total"], max_connections)
        self.assertEqual(stats["used_percentage"], round(stats["total"] / max_connections * 100, 2))

    def test_background_workers_are_left_out_of_the_count(self):
        # Autovacuum, walwriter and friends show up in pg_stat_activity but hold no
        # max_connections slot, so counting them would inflate the ratio.
        #
        # IS DISTINCT FROM rather than <> because backend_type is one of the columns
        # pg_stat_activity masks on rows the querying role does not own, and the
        # auxiliary processes are owned by no role at all. A superuser sees their real
        # backend_type, anyone else sees null, and either way those rows are ones the
        # collector must not count.
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FILTER (WHERE backend_type IS DISTINCT FROM 'client backend'), count(*) FROM pg_stat_activity")
            non_client_backends, all_backends = cursor.fetchone()

        self.assertGreater(non_client_backends, 0, "Postgres always runs background workers, without them this test cannot tell the two counts apart")
        self.assertLessEqual(get_database_connection_stats()["total"], all_backends - non_client_backends)

    def test_an_unreadable_ceiling_leaves_the_percentage_unreported_rather_than_dividing_by_zero(self):
        with patch(f"{MODULE}._fetch_with_timeout", return_value=[(5, 0)]):
            self.assertIsNone(get_database_connection_stats()["used_percentage"])

    def test_is_cheap_enough_to_run_on_every_scheduler_cycle(self):
        self.assertFasterThan(CONNECTION_STATS_BUDGET_SECONDS, get_database_connection_stats)

    def test_costs_a_fixed_number_of_statements(self):
        with CaptureQueriesContext(connection) as captured:
            get_database_connection_stats()

        # The timeout is set through a bind parameter, so it is a statement of its own.
        self.assertEqual(len(_statements_excluding_savepoints(captured.captured_queries)), 2)


class GetDatabaseTableSizesTestCase(TimingAssertionsMixin, TestCase):
    def test_reports_a_real_size_for_a_known_table(self):
        tables = get_database_table_sizes()["tables"]

        self.assertIn(f"public.{SNAPSHOT_TABLE}", tables)
        self.assertGreater(tables[f"public.{SNAPSHOT_TABLE}"], 0)

    def test_catalog_schemas_are_left_out(self):
        tables = get_database_table_sizes()["tables"]

        self.assertEqual([name for name in tables if name.startswith(("pg_catalog.", "information_schema."))], [])

    def test_the_total_is_the_sum_of_the_tables(self):
        sizes = get_database_table_sizes()

        self.assertEqual(sizes["total_bytes"], sum(sizes["tables"].values()))

    def test_tables_come_back_largest_first(self):
        sizes = list(get_database_table_sizes()["tables"].values())

        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_a_table_reporting_no_size_is_dropped_rather_than_counted_as_zero(self):
        with patch(f"{MODULE}._fetch_with_timeout", return_value=[("public", "sized", 100), ("public", "unsized", None)]):
            sizes = get_database_table_sizes()

        self.assertEqual(sizes, {"total_bytes": 100, "tables": {"public.sized": 100}})

    def test_is_cheap_enough_for_the_interval_it_is_sampled_on(self):
        self.assertFasterThan(TABLE_SIZES_BUDGET_SECONDS, get_database_table_sizes)

    def test_every_table_is_measured_by_a_single_statement(self):
        # A query per table would satisfy every assertion above while turning one
        # scan of pg_class into hundreds of round trips.
        with CaptureQueriesContext(connection) as captured:
            table_count = len(get_database_table_sizes()["tables"])

        self.assertGreater(table_count, 10)
        self.assertEqual(len(_statements_excluding_savepoints(captured.captured_queries)), 2)


class MetricStatementTimeoutTestCase(TestCase):
    def test_the_configured_timeout_is_applied_to_the_query(self):
        with patch(f"{MODULE}.INSTANCE_HEALTH_METRIC_STATEMENT_TIMEOUT_MS", 250):
            self.assertEqual(_fetch_with_timeout("SHOW statement_timeout"), [("250ms",)])

    def test_a_slow_query_is_cancelled_instead_of_stalling_the_caller(self):
        # These run against a database that may already be struggling, which is both
        # when the numbers are most worth having and when waiting for them is worst.
        with patch(f"{MODULE}.INSTANCE_HEALTH_METRIC_STATEMENT_TIMEOUT_MS", 100):
            started = time.perf_counter()
            with self.assertRaises(OperationalError):
                _fetch_with_timeout("SELECT pg_sleep(30)")
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 5, f"The query was not cancelled promptly, it took {elapsed:.1f}s")


class MetricStatementTimeoutScopeTestCase(TransactionTestCase):
    """TransactionTestCase because TestCase wraps each test in a transaction of its own.

    The timeout is transaction local, so under TestCase it would be scoped to the
    test's wrapping transaction rather than to the metric query, and a timeout left
    behind on the connection would go unnoticed.
    """

    def test_the_timeout_does_not_outlive_the_query(self):
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            before = cursor.fetchone()[0]

        _fetch_with_timeout(CONNECTION_STATS_SQL)

        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            self.assertEqual(cursor.fetchone()[0], before)


class DeleteSnapshotsOutsideRetentionWindowTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.cutoff = self.now - timedelta(seconds=INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS)

    def test_deletes_what_is_outside_the_window_and_keeps_what_is_inside(self):
        outside = _create_snapshot(created_at=self.cutoff - timedelta(minutes=1))
        inside = _create_snapshot(created_at=self.cutoff + timedelta(minutes=1))

        self.assertEqual(delete_snapshots_outside_retention_window(), 1)
        self.assertFalse(InstanceHealthSnapshot.objects.filter(pk=outside.pk).exists())
        self.assertTrue(InstanceHealthSnapshot.objects.filter(pk=inside.pk).exists())

    def test_an_empty_table_is_a_noop(self):
        self.assertEqual(delete_snapshots_outside_retention_window(), 0)

    def test_a_table_of_snapshots_inside_the_window_is_left_alone(self):
        _create_snapshots(5, created_at=self.now)

        self.assertEqual(delete_snapshots_outside_retention_window(), 0)
        self.assertEqual(InstanceHealthSnapshot.objects.count(), 5)

    def test_a_backlog_is_capped_per_pass_and_drained_oldest_first(self):
        # Created oldest first so that id order matches age order, the way the
        # scheduler writes them.
        snapshots = [_create_snapshot(created_at=self.cutoff - timedelta(days=10) + timedelta(minutes=minute)) for minute in range(10)]

        with patch(f"{MODULE}.INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE", 2), patch(f"{MODULE}.INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS", 3):
            deleted = delete_snapshots_outside_retention_window()

        self.assertEqual(deleted, 6)
        self.assertEqual(list(InstanceHealthSnapshot.objects.order_by("id").values_list("id", flat=True)), [snapshot.id for snapshot in snapshots[6:]])

    def test_a_backlog_too_large_for_one_pass_is_drained_over_several(self):
        # A retention window that was just shortened, or a table that predates this
        # cleanup, should not turn into one long transaction.
        _create_snapshots(10, created_at=self.cutoff - timedelta(days=1))

        with patch(f"{MODULE}.INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE", 2), patch(f"{MODULE}.INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS", 3):
            passes = [delete_snapshots_outside_retention_window() for _ in range(3)]

        self.assertEqual(passes, [6, 4, 0])
        self.assertEqual(InstanceHealthSnapshot.objects.count(), 0)

    def test_a_short_backlog_stops_early_rather_than_running_every_batch(self):
        _create_snapshot(created_at=self.cutoff - timedelta(days=1))

        with CaptureQueriesContext(connection) as captured:
            delete_snapshots_outside_retention_window()

        # One select and one delete for the batch that had a row, then one select
        # that comes back empty and ends the pass.
        self.assertEqual(len(_statements_touching_snapshots(captured.captured_queries)), 3)

    def test_a_full_pass_stays_bounded_with_a_large_backlog(self):
        per_pass_cap = INSTANCE_HEALTH_SNAPSHOT_CLEANUP_BATCH_SIZE * INSTANCE_HEALTH_SNAPSHOT_CLEANUP_MAX_BATCHES_PER_PASS
        _create_snapshots(per_pass_cap + 100, created_at=self.cutoff - timedelta(days=1))

        started = time.perf_counter()
        deleted = delete_snapshots_outside_retention_window()
        elapsed = time.perf_counter() - started

        self.assertEqual(deleted, per_pass_cap)
        self.assertEqual(InstanceHealthSnapshot.objects.count(), 100)
        self.assertLess(elapsed, CLEANUP_PASS_BUDGET_SECONDS, f"Deleting {per_pass_cap} snapshots took {elapsed:.2f}s, budget is {CLEANUP_PASS_BUDGET_SECONDS:.0f}s")


@override_settings(SAVE_INSTANCE_HEALTH_SNAPSHOTS=True)
class SaveSnapshotIfNeededTestCase(TimingAssertionsMixin, TestCase):
    def setUp(self):
        self.taker = InstanceHealthSnapshotTaker()

        # Redis and Celery are the only collectors that reach outside the database,
        # so they are the only ones stood in for; the rest run real SQL.
        self.taker._redis_client = MagicMock()
        self.taker._redis_client.llen.return_value = 0

        inspect_patcher = patch(f"{MODULE}.celery_app.control.inspect")
        self.mock_inspect = inspect_patcher.start()
        self.addCleanup(inspect_patcher.stop)
        self.mock_inspect.return_value.stats.return_value = {"celery@one": {"pool": {"max-concurrency": 4, "processes": [1, 2, 3, 4]}, "prefetch_count": 16}}

    # The taker throttles on time.monotonic(), so rewinding the clock it recorded is
    # the same thing as waiting, without the wait or a patched clock.
    def _make_snapshot_due(self):
        self.taker._last_snapshot_time -= INSTANCE_HEALTH_SNAPSHOT_INTERVAL_SECONDS + 1

    def _make_table_sizes_due(self):
        self.taker._last_table_size_sample_time -= INSTANCE_HEALTH_TABLE_SIZE_INTERVAL_SECONDS + 1

    def _make_worker_stats_due(self):
        self.taker._last_celery_worker_stats_sample_time -= INSTANCE_HEALTH_CELERY_WORKER_STATS_INTERVAL_SECONDS + 1

    def _make_cleanup_due(self):
        self.taker._last_cleanup_time -= INSTANCE_HEALTH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS + 1

    def _latest_snapshot_data(self):
        return InstanceHealthSnapshot.objects.order_by("-id").first().data

    @override_settings(SAVE_INSTANCE_HEALTH_SNAPSHOTS=False)
    def test_nothing_is_written_unless_snapshotting_is_switched_on(self):
        self.taker.save_snapshot_if_needed()

        self.assertEqual(InstanceHealthSnapshot.objects.count(), 0)

    def test_the_first_snapshot_carries_every_metric(self):
        self.taker.save_snapshot_if_needed()

        data = self._latest_snapshot_data()
        self.assertEqual(set(data), {"celery_queue_depths", "celery_worker_stats", "database_connections", "database_table_sizes"})
        self.assertEqual(data["celery_worker_stats"]["worker_count"], 1)
        self.assertGreaterEqual(data["database_connections"]["total"], 1)
        self.assertGreater(data["database_table_sizes"]["total_bytes"], 0)

    def test_a_second_call_inside_the_interval_does_nothing(self):
        self.taker.save_snapshot_if_needed()
        self.taker.save_snapshot_if_needed()

        self.assertEqual(InstanceHealthSnapshot.objects.count(), 1)

    def test_a_call_after_the_interval_has_elapsed_writes_another_snapshot(self):
        self.taker.save_snapshot_if_needed()
        self._make_snapshot_due()
        self.taker.save_snapshot_if_needed()

        self.assertEqual(InstanceHealthSnapshot.objects.count(), 2)

    def test_the_expensive_metrics_are_left_off_snapshots_they_are_not_due_on(self):
        self.taker.save_snapshot_if_needed()
        self._make_snapshot_due()
        self.taker.save_snapshot_if_needed()

        self.assertEqual(set(self._latest_snapshot_data()), {"celery_queue_depths", "database_connections"})

    def test_an_expensive_metric_lands_on_whichever_snapshot_is_due_when_it_comes_round(self):
        self.taker.save_snapshot_if_needed()

        self._make_snapshot_due()
        self._make_table_sizes_due()
        self.taker.save_snapshot_if_needed()
        self.assertIn("database_table_sizes", self._latest_snapshot_data())
        self.assertNotIn("celery_worker_stats", self._latest_snapshot_data())

        self._make_snapshot_due()
        self._make_worker_stats_due()
        self.taker.save_snapshot_if_needed()
        self.assertIn("celery_worker_stats", self._latest_snapshot_data())
        self.assertNotIn("database_table_sizes", self._latest_snapshot_data())

    def test_a_failing_collector_does_not_take_the_rest_of_the_snapshot_with_it(self):
        # A partial snapshot beats none, and these numbers matter most when
        # something is already broken.
        with patch(f"{MODULE}.get_database_table_sizes", side_effect=Exception("canceling statement due to statement timeout")):
            with self.assertLogs(MODULE, level="ERROR"):
                self.taker.save_snapshot_if_needed()

        self.assertEqual(set(self._latest_snapshot_data()), {"celery_queue_depths", "celery_worker_stats", "database_connections"})

    def test_a_failing_collector_is_not_retried_until_it_is_next_due(self):
        with patch(f"{MODULE}.get_database_table_sizes", side_effect=Exception("canceling statement due to statement timeout")) as mock_table_sizes:
            with self.assertLogs(MODULE, level="ERROR"):
                self.taker.save_snapshot_if_needed()
            self._make_snapshot_due()
            self.taker.save_snapshot_if_needed()

        self.assertEqual(mock_table_sizes.call_count, 1)

    def test_no_snapshot_is_written_when_every_collector_fails(self):
        failure = Exception("everything is on fire")
        with (
            patch(f"{MODULE}.get_celery_queue_depths", side_effect=failure),
            patch(f"{MODULE}.get_celery_worker_stats", side_effect=failure),
            patch(f"{MODULE}.get_database_connection_stats", side_effect=failure),
            patch(f"{MODULE}.get_database_table_sizes", side_effect=failure),
        ):
            with self.assertLogs(MODULE, level="ERROR"):
                self.taker.save_snapshot_if_needed()

        self.assertEqual(InstanceHealthSnapshot.objects.count(), 0)

    def test_the_redis_client_is_dropped_when_the_queue_read_fails(self):
        # A client left holding a broken connection would otherwise keep failing for
        # the lifetime of the scheduler.
        with patch(f"{MODULE}.get_celery_queue_depths", side_effect=ConnectionError("redis is down")):
            with self.assertLogs(MODULE, level="ERROR"):
                self.taker.save_snapshot_if_needed()

        self.assertIsNone(self.taker._redis_client)
        self.assertIn("database_connections", self._latest_snapshot_data())

    def test_a_failed_write_is_swallowed_rather_than_raised_at_the_scheduler(self):
        with patch.object(InstanceHealthSnapshot.objects, "create", side_effect=Exception("no space left on device")):
            with self.assertLogs(MODULE, level="ERROR"):
                self.taker.save_snapshot_if_needed()

    def test_snapshots_outside_the_retention_window_are_removed_as_it_goes(self):
        stale = _create_snapshot(created_at=timezone.now() - timedelta(seconds=INSTANCE_HEALTH_SNAPSHOT_RETENTION_SECONDS + 60))

        self.taker.save_snapshot_if_needed()

        self.assertFalse(InstanceHealthSnapshot.objects.filter(pk=stale.pk).exists())
        self.assertEqual(InstanceHealthSnapshot.objects.count(), 1)

    def test_cleanup_runs_on_its_own_interval_rather_than_on_every_snapshot(self):
        with patch(f"{MODULE}.delete_snapshots_outside_retention_window", return_value=0) as mock_cleanup:
            self.taker.save_snapshot_if_needed()
            self._make_snapshot_due()
            self.taker.save_snapshot_if_needed()
            self.assertEqual(mock_cleanup.call_count, 1)

            self._make_snapshot_due()
            self._make_cleanup_due()
            self.taker.save_snapshot_if_needed()
            self.assertEqual(mock_cleanup.call_count, 2)

    def test_a_failed_cleanup_still_leaves_a_snapshot_behind(self):
        with patch(f"{MODULE}.delete_snapshots_outside_retention_window", side_effect=Exception("deadlock detected")):
            with self.assertLogs(MODULE, level="ERROR"):
                self.taker.save_snapshot_if_needed()

        self.assertEqual(InstanceHealthSnapshot.objects.count(), 1)

    def test_a_failed_cleanup_is_not_retried_until_it_is_next_due(self):
        with patch(f"{MODULE}.delete_snapshots_outside_retention_window", side_effect=Exception("deadlock detected")) as mock_cleanup:
            with self.assertLogs(MODULE, level="ERROR"):
                self.taker.save_snapshot_if_needed()
            self._make_snapshot_due()
            self.taker.save_snapshot_if_needed()

        self.assertEqual(mock_cleanup.call_count, 1)

    def test_a_due_snapshot_fits_inside_the_scheduler_loop(self):
        def take_a_full_snapshot():
            self.taker._last_snapshot_time = None
            self.taker._last_table_size_sample_time = None
            self.taker._last_celery_worker_stats_sample_time = None
            self.taker._last_cleanup_time = None
            self.taker.save_snapshot_if_needed()

        self.assertFasterThan(FULL_SNAPSHOT_BUDGET_SECONDS, take_a_full_snapshot, runs=3)

    def test_the_cost_of_a_snapshot_does_not_grow_with_the_table(self):
        # The scheduler writes a row per interval forever, so anything here that
        # scanned the table rather than using the created_at index would degrade
        # quietly over weeks instead of failing outright.
        with CaptureQueriesContext(connection) as captured:
            self.taker.save_snapshot_if_needed()
        on_an_empty_table = len(captured.captured_queries)

        _create_snapshots(2000, created_at=timezone.now())
        self._make_snapshot_due()
        self._make_table_sizes_due()
        self._make_worker_stats_due()
        self._make_cleanup_due()

        with CaptureQueriesContext(connection) as captured:
            started = time.perf_counter()
            self.taker.save_snapshot_if_needed()
            elapsed = time.perf_counter() - started

        self.assertEqual(len(captured.captured_queries), on_an_empty_table)
        self.assertLess(elapsed, FULL_SNAPSHOT_BUDGET_SECONDS, f"A snapshot taken over a table of 2000 rows took {elapsed * 1000:.1f}ms, budget is {FULL_SNAPSHOT_BUDGET_SECONDS * 1000:.0f}ms")
