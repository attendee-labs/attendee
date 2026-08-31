"""Tests for bots.instance_health_alert_manager.

The firing decisions are exercised against real snapshot rows rather than a stubbed
reader, because what an alert claims is a claim about snapshot history: that a metric
has been over its threshold, or that a queue has failed to drain, for as long as the
operator asked. A stub would let that history be assumed rather than tested. The two
Celery evaluators are only stood in for where the test is about which threshold
reaches them rather than about what they conclude from it.

Reconciliation (which alerts flipped, what is written, what is sent to Slack) is
tested with the firing decision stubbed instead, so those tests fail for reasons to do
with state handling rather than with any particular metric.
"""

import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from bots.instance_health_alert_manager import (
    ALERT_METADATA,
    BYTES_PER_GIGABYTE,
    InstanceHealthAlertManager,
    InstanceHealthAlertTypes,
    _notify_slack_of_alert_changes,
    _threshold_from_display,
    _threshold_to_display,
    compute_active_alerts,
    get_alert_configs,
    update_alert_settings,
)
from bots.instance_health_utils import CONNECTION_DANGER_PERCENTAGE, WORKER_SILENCE_CONFIRMED_AFTER_SECONDS
from bots.models import InstanceHealthAlertsState, InstanceHealthSnapshot

# Doubles as the patch target for the module's own globals and as the name of the
# logger it reports state changes and failures on.
MODULE = "bots.instance_health_alert_manager"

SLACK_WEBHOOK_ENV_VAR = "SLACK_WEBHOOK_URL_FOR_INSTANCE_HEALTH_ALERTS"

CONNECTIONS = InstanceHealthAlertTypes.CONNECTIONS_USED_PERCENTAGE_EXCEEDS_THRESHOLD
DATABASE_SIZE = InstanceHealthAlertTypes.DATABASE_SIZE_EXCEEDS_THRESHOLD
WORKERS_DOWN = InstanceHealthAlertTypes.CELERY_WORKERS_DOWN
QUEUE_NOT_DRAINING = InstanceHealthAlertTypes.CELERY_QUEUE_NOT_DRAINING

NO_WORKERS = {"worker_count": 0, "workers": {}}
ONE_WORKER = {"worker_count": 1, "workers": {"celery@one": {"concurrency": 4, "processes_alive": 4}}}


def _create_snapshot(data, created_at=None):
    """Create one snapshot, optionally backdated.

    created_at is auto_now_add, which ignores assignment at create time, so it has to
    be set with an update() afterwards.
    """
    snapshot = InstanceHealthSnapshot.objects.create(data=data)
    if created_at is not None:
        InstanceHealthSnapshot.objects.filter(pk=snapshot.pk).update(created_at=created_at)
    return snapshot


def _settings_for(alert, **config):
    """Settings in which alert is the only enabled one.

    compute_active_alerts always evaluates every alert, so leaving the others enabled
    would let an unrelated metric decide the assertion.
    """
    alert_settings = {other.value: {"enabled": False} for other in InstanceHealthAlertTypes}
    alert_settings[alert.value] = {"enabled": True, **config}
    return alert_settings


def _state_with(active_alerts):
    """The firing state the manager writes for a given set of active alerts."""
    return {alert.value: {"active": alert in active_alerts} for alert in InstanceHealthAlertTypes}


def _alerts_state(settings=None, state=None):
    """An unsaved singleton row: get_alert_configs only reads these two fields."""
    return InstanceHealthAlertsState(settings=settings or {}, state=state or {})


class SingleAlertMixin:
    """Assertions about one alert evaluated with the other three switched off."""

    alert = None

    def assertFiring(self, **config):
        self.assertEqual(compute_active_alerts(_settings_for(self.alert, **config)), {self.alert})

    def assertNotFiring(self, **config):
        self.assertEqual(compute_active_alerts(_settings_for(self.alert, **config)), set())


class ComputeActiveAlertsTestCase(TestCase):
    def setUp(self):
        _create_snapshot({"database_connections": {"total": 95, "max_connections": 100, "used_percentage": 95.0}})

    def test_an_alert_with_no_stored_configuration_is_enabled_with_its_default_threshold(self):
        # A deployment that has never opened the settings form still gets alerted.
        self.assertEqual(compute_active_alerts({}), {CONNECTIONS})

    def test_a_disabled_alert_is_never_active_however_its_metric_is_behaving(self):
        self.assertEqual(compute_active_alerts({CONNECTIONS.value: {"enabled": False}}), set())

    def test_a_stored_configuration_missing_a_threshold_falls_back_to_the_default(self):
        self.assertEqual(compute_active_alerts({CONNECTIONS.value: {"enabled": True}}), {CONNECTIONS})

    def test_alerts_are_evaluated_independently_of_one_another(self):
        _create_snapshot({"database_table_sizes": {"total_bytes": 200 * BYTES_PER_GIGABYTE, "tables": {}}})

        active = compute_active_alerts({CONNECTIONS.value: {"enabled": True}, DATABASE_SIZE.value: {"enabled": False}})

        self.assertEqual(active, {CONNECTIONS})

    def test_nothing_is_active_when_no_snapshots_have_been_taken(self):
        InstanceHealthSnapshot.objects.all().delete()

        self.assertEqual(compute_active_alerts({}), set())


class ConnectionsAlertTestCase(SingleAlertMixin, TestCase):
    alert = CONNECTIONS

    def _create_reading(self, used_percentage, created_at=None):
        return _create_snapshot({"database_connections": {"total": 50, "max_connections": 100, "used_percentage": used_percentage}}, created_at=created_at)

    def test_usage_over_the_threshold_fires(self):
        self._create_reading(90.0)

        self.assertFiring(threshold=85)

    def test_usage_exactly_at_the_threshold_fires(self):
        # The threshold is the point at which an operator asked to hear about it, so
        # landing on it is over it.
        self._create_reading(85.0)

        self.assertFiring(threshold=85)

    def test_usage_under_the_threshold_does_not_fire(self):
        self._create_reading(84.9)

        self.assertNotFiring(threshold=85)

    def test_the_default_threshold_is_the_dashboard_danger_line(self):
        # Otherwise the dashboard would colour a panel red without an alert firing.
        self._create_reading(float(CONNECTION_DANGER_PERCENTAGE))

        self.assertFiring()

    def test_a_reading_with_no_percentage_does_not_fire(self):
        # used_percentage is null when max_connections could not be read, which says
        # nothing about how much of the pool is in use.
        self._create_reading(None)

        self.assertNotFiring(threshold=85)

    def test_only_the_most_recent_reading_decides(self):
        now = timezone.now()
        self._create_reading(95.0, created_at=now - timedelta(minutes=5))
        self._create_reading(10.0, created_at=now)

        self.assertNotFiring(threshold=85)


class DatabaseSizeAlertTestCase(SingleAlertMixin, TestCase):
    alert = DATABASE_SIZE

    def _create_reading(self, total_bytes, created_at=None):
        return _create_snapshot({"database_table_sizes": {"total_bytes": total_bytes, "tables": {"public.bots_bot": total_bytes}}}, created_at=created_at)

    def test_a_database_over_the_threshold_fires(self):
        self._create_reading(120 * BYTES_PER_GIGABYTE)

        self.assertFiring(threshold=100 * BYTES_PER_GIGABYTE)

    def test_a_database_exactly_at_the_threshold_fires(self):
        self._create_reading(100 * BYTES_PER_GIGABYTE)

        self.assertFiring(threshold=100 * BYTES_PER_GIGABYTE)

    def test_a_database_under_the_threshold_does_not_fire(self):
        self._create_reading(99 * BYTES_PER_GIGABYTE)

        self.assertNotFiring(threshold=100 * BYTES_PER_GIGABYTE)

    def test_the_reading_is_taken_from_the_last_snapshot_that_carried_it(self):
        # Table sizes are sampled on a much longer interval than snapshots are
        # written, so the newest snapshot usually does not carry them at all.
        now = timezone.now()
        self._create_reading(120 * BYTES_PER_GIGABYTE, created_at=now - timedelta(hours=1))
        _create_snapshot({"database_connections": {"total": 1, "max_connections": 100, "used_percentage": 1.0}}, created_at=now)

        self.assertFiring(threshold=100 * BYTES_PER_GIGABYTE)


class CeleryWorkersDownAlertTestCase(SingleAlertMixin, TestCase):
    alert = WORKERS_DOWN

    def test_a_pool_that_has_been_down_across_the_whole_threshold_fires(self):
        now = timezone.now()
        _create_snapshot({"celery_worker_stats": NO_WORKERS}, created_at=now - timedelta(seconds=601))
        _create_snapshot({"celery_worker_stats": NO_WORKERS}, created_at=now)

        self.assertFiring(threshold=600)

    def test_a_pool_that_came_back_inside_the_threshold_does_not_fire(self):
        # A gap of a census or two is what a deploy looks like from here.
        now = timezone.now()
        _create_snapshot({"celery_worker_stats": NO_WORKERS}, created_at=now - timedelta(seconds=601))
        _create_snapshot({"celery_worker_stats": ONE_WORKER}, created_at=now - timedelta(seconds=300))
        _create_snapshot({"celery_worker_stats": NO_WORKERS}, created_at=now)

        self.assertNotFiring(threshold=600)

    def test_a_history_too_short_to_cover_the_threshold_does_not_fire(self):
        _create_snapshot({"celery_worker_stats": NO_WORKERS})

        self.assertNotFiring(threshold=600)

    def test_the_configured_threshold_is_what_is_evaluated(self):
        with patch(f"{MODULE}.celery_workers_have_been_down_for", return_value=True) as mock_have_been_down:
            self.assertFiring(threshold=777)

        mock_have_been_down.assert_called_once_with(777)

    def test_the_default_threshold_is_the_dashboard_silence_grace_period(self):
        # The panel and the alert should agree on when silence becomes an outage.
        with patch(f"{MODULE}.celery_workers_have_been_down_for", return_value=True) as mock_have_been_down:
            self.assertFiring()

        mock_have_been_down.assert_called_once_with(WORKER_SILENCE_CONFIRMED_AFTER_SECONDS)


class CeleryQueueNotDrainingAlertTestCase(SingleAlertMixin, TestCase):
    alert = QUEUE_NOT_DRAINING

    def _create_readings(self, oldest, middle, latest):
        """Three readings for one queue, the oldest of them older than a 600 second threshold."""
        now = timezone.now()
        for depth, seconds_ago in [(oldest, 700), (middle, 350), (latest, 0)]:
            _create_snapshot({"celery_queue_depths": {"bots": depth}}, created_at=now - timedelta(seconds=seconds_ago))

    def test_a_backlog_that_only_grew_across_the_threshold_fires(self):
        self._create_readings(5, 5, 9)

        self.assertFiring(threshold=600)

    def test_a_backlog_that_dropped_at_any_point_does_not_fire(self):
        self._create_readings(9, 7, 8)

        self.assertNotFiring(threshold=600)

    def test_an_empty_queue_is_idle_rather_than_stalled(self):
        self._create_readings(0, 0, 0)

        self.assertNotFiring(threshold=600)

    def test_the_configured_threshold_is_what_is_evaluated(self):
        with patch(f"{MODULE}.celery_queue_has_not_decreased_for", return_value=True) as mock_has_not_decreased:
            self.assertFiring(threshold=777)

        mock_has_not_decreased.assert_called_once_with(777)


class GetAlertConfigsTestCase(TestCase):
    def _configs_by_key(self, alerts_state):
        return {config["key"]: config for config in get_alert_configs(alerts_state)}

    def test_every_alert_is_described_once(self):
        configs = get_alert_configs(_alerts_state())

        self.assertEqual([config["key"] for config in configs], [alert.value for alert in InstanceHealthAlertTypes])

    def test_an_unconfigured_alert_is_described_by_its_defaults(self):
        configs = self._configs_by_key(_alerts_state())

        self.assertTrue(all(config["enabled"] for config in configs.values()))
        self.assertFalse(any(config["active"] for config in configs.values()))
        self.assertEqual(configs[CONNECTIONS.value]["threshold"], CONNECTION_DANGER_PERCENTAGE)
        self.assertEqual(configs[DATABASE_SIZE.value]["threshold"], 100)
        self.assertEqual(configs[WORKERS_DOWN.value]["threshold"], WORKER_SILENCE_CONFIRMED_AFTER_SECONDS / 60)

    def test_thresholds_are_reported_in_the_unit_the_form_asks_for(self):
        alerts_state = _alerts_state(settings={DATABASE_SIZE.value: {"enabled": True, "threshold": 250 * BYTES_PER_GIGABYTE}, WORKERS_DOWN.value: {"enabled": True, "threshold": 900}})

        configs = self._configs_by_key(alerts_state)

        self.assertEqual(configs[DATABASE_SIZE.value]["threshold"], 250)
        self.assertEqual(configs[WORKERS_DOWN.value]["threshold"], 15)

    def test_a_whole_threshold_is_reported_without_a_decimal_point(self):
        # The form pre-fills from this, and "100" is what an operator typed.
        configs = self._configs_by_key(_alerts_state(settings={DATABASE_SIZE.value: {"threshold": 100 * BYTES_PER_GIGABYTE}}))

        self.assertIsInstance(configs[DATABASE_SIZE.value]["threshold"], int)

    def test_a_fractional_threshold_keeps_its_fraction(self):
        configs = self._configs_by_key(_alerts_state(settings={DATABASE_SIZE.value: {"threshold": int(2.5 * BYTES_PER_GIGABYTE)}}))

        self.assertEqual(configs[DATABASE_SIZE.value]["threshold"], 2.5)

    def test_a_threshold_that_does_not_divide_evenly_is_rounded_for_display(self):
        configs = self._configs_by_key(_alerts_state(settings={DATABASE_SIZE.value: {"threshold": BYTES_PER_GIGABYTE // 3}}))

        self.assertEqual(configs[DATABASE_SIZE.value]["threshold"], 0.33)

    def test_a_disabled_alert_is_reported_as_disabled(self):
        configs = self._configs_by_key(_alerts_state(settings={QUEUE_NOT_DRAINING.value: {"enabled": False}}))

        self.assertFalse(configs[QUEUE_NOT_DRAINING.value]["enabled"])
        self.assertTrue(configs[CONNECTIONS.value]["enabled"])

    def test_a_firing_alert_is_reported_as_active(self):
        configs = self._configs_by_key(_alerts_state(state={WORKERS_DOWN.value: {"active": True}}))

        self.assertTrue(configs[WORKERS_DOWN.value]["active"])
        self.assertFalse(configs[CONNECTIONS.value]["active"])

    def test_each_alert_carries_the_labelling_the_form_renders(self):
        configs = self._configs_by_key(_alerts_state())

        for alert, metadata in ALERT_METADATA.items():
            self.assertEqual(configs[alert.value]["label"], metadata["label"])
            self.assertEqual(configs[alert.value]["description"], metadata["description"])
            self.assertEqual(configs[alert.value]["unit_label"], metadata["unit_label"])
            self.assertEqual(configs[alert.value]["step"], metadata["step"])


class ThresholdConversionTestCase(TestCase):
    def test_a_typed_threshold_is_stored_in_the_unit_the_alert_evaluates(self):
        self.assertEqual(_threshold_from_display(DATABASE_SIZE, 2.5), int(2.5 * BYTES_PER_GIGABYTE))
        self.assertEqual(_threshold_from_display(WORKERS_DOWN, 5), 300)
        self.assertEqual(_threshold_from_display(CONNECTIONS, 90), 90)

    def test_a_whole_stored_threshold_stays_an_integer(self):
        # Seconds and byte counts are compared against integer metrics, and a float
        # would also show up in the JSON column as one.
        self.assertIsInstance(_threshold_from_display(WORKERS_DOWN, 5.0), int)
        self.assertIsInstance(_threshold_from_display(DATABASE_SIZE, 1.5), int)

    def test_a_threshold_survives_a_round_trip_through_storage(self):
        for alert, display_value in [(CONNECTIONS, 90), (DATABASE_SIZE, 2.5), (WORKERS_DOWN, 20), (QUEUE_NOT_DRAINING, 15)]:
            with self.subTest(alert=alert.value):
                self.assertEqual(_threshold_to_display(alert, _threshold_from_display(alert, display_value)), display_value)


class UpdateAlertSettingsTestCase(TestCase):
    def setUp(self):
        self.alerts_state = InstanceHealthAlertsState.load()

    def _stored_settings(self):
        return InstanceHealthAlertsState.load().settings

    def _seed_settings(self, alert_settings):
        self.alerts_state.settings = alert_settings
        self.alerts_state.save(update_fields=["settings", "updated_at"])

    def test_a_submitted_threshold_is_stored_in_the_unit_the_alert_evaluates(self):
        update_alert_settings(self.alerts_state, {f"{DATABASE_SIZE.value}__enabled": "on", f"{DATABASE_SIZE.value}__threshold": "2.5", f"{WORKERS_DOWN.value}__enabled": "on", f"{WORKERS_DOWN.value}__threshold": "5"})

        stored = self._stored_settings()
        self.assertEqual(stored[DATABASE_SIZE.value]["threshold"], int(2.5 * BYTES_PER_GIGABYTE))
        self.assertEqual(stored[WORKERS_DOWN.value]["threshold"], 300)

    def test_a_checked_box_enables_its_alert(self):
        update_alert_settings(self.alerts_state, {f"{CONNECTIONS.value}__enabled": "on"})

        self.assertTrue(self._stored_settings()[CONNECTIONS.value]["enabled"])

    def test_an_alert_left_out_of_the_form_is_disabled(self):
        # An unchecked checkbox is absent from the POST rather than sent as false, so
        # every alert has to be rewritten from the form for that to read as disabled.
        self._seed_settings({CONNECTIONS.value: {"enabled": True, "threshold": 85}})

        update_alert_settings(self.alerts_state, {})

        self.assertFalse(any(config["enabled"] for config in self._stored_settings().values()))

    def test_every_alert_is_written_even_when_the_form_mentions_none_of_them(self):
        update_alert_settings(self.alerts_state, {})

        self.assertEqual(set(self._stored_settings()), {alert.value for alert in InstanceHealthAlertTypes})

    def test_a_blank_threshold_leaves_the_stored_one_alone(self):
        # Clearing the box should not be read as an instruction to alert at zero.
        self._seed_settings({WORKERS_DOWN.value: {"enabled": True, "threshold": 999}})

        update_alert_settings(self.alerts_state, {f"{WORKERS_DOWN.value}__enabled": "on", f"{WORKERS_DOWN.value}__threshold": ""})

        self.assertEqual(self._stored_settings()[WORKERS_DOWN.value]["threshold"], 999)

    def test_an_unparseable_threshold_leaves_the_stored_one_alone(self):
        self._seed_settings({WORKERS_DOWN.value: {"enabled": True, "threshold": 999}})

        update_alert_settings(self.alerts_state, {f"{WORKERS_DOWN.value}__enabled": "on", f"{WORKERS_DOWN.value}__threshold": "soon"})

        self.assertEqual(self._stored_settings()[WORKERS_DOWN.value]["threshold"], 999)

    def test_an_alert_never_configured_before_keeps_its_default_threshold(self):
        update_alert_settings(self.alerts_state, {f"{CONNECTIONS.value}__enabled": "on"})

        self.assertEqual(self._stored_settings()[CONNECTIONS.value]["threshold"], CONNECTION_DANGER_PERCENTAGE)

    def test_saving_settings_does_not_disturb_the_firing_state(self):
        # The manager writes state on its own cadence, and an operator editing
        # configuration must not race it into reporting a stale firing state.
        self.alerts_state.state = _state_with([WORKERS_DOWN])
        self.alerts_state.save(update_fields=["state", "updated_at"])

        update_alert_settings(self.alerts_state, {})

        self.assertEqual(InstanceHealthAlertsState.load().state, _state_with([WORKERS_DOWN]))

    def test_the_saved_settings_are_what_the_form_reads_back(self):
        update_alert_settings(self.alerts_state, {f"{DATABASE_SIZE.value}__enabled": "on", f"{DATABASE_SIZE.value}__threshold": "250"})

        configs = {config["key"]: config for config in get_alert_configs(InstanceHealthAlertsState.load())}
        self.assertEqual(configs[DATABASE_SIZE.value]["threshold"], 250)
        self.assertTrue(configs[DATABASE_SIZE.value]["enabled"])


@override_settings(SAVE_INSTANCE_HEALTH_SNAPSHOTS=True)
class UpdateAlertsTestCase(TestCase):
    def setUp(self):
        self.manager = InstanceHealthAlertManager()

        # Which alerts are firing is covered against real snapshots above; these
        # tests are about what the manager does with that answer.
        compute_patcher = patch(f"{MODULE}.compute_active_alerts", return_value=set())
        self.mock_compute = compute_patcher.start()
        self.addCleanup(compute_patcher.stop)

        notify_patcher = patch(f"{MODULE}._notify_slack_of_alert_changes")
        self.mock_notify = notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

    def _set_firing(self, *alerts):
        self.mock_compute.return_value = set(alerts)

    def _seed_state(self, state):
        alerts_state = InstanceHealthAlertsState.load()
        alerts_state.state = state
        alerts_state.save(update_fields=["state", "updated_at"])

    def _stored_state(self):
        return InstanceHealthAlertsState.load().state

    @override_settings(SAVE_INSTANCE_HEALTH_SNAPSHOTS=False)
    def test_nothing_is_evaluated_unless_snapshotting_is_switched_on(self):
        # Alerts read the snapshots, so with none being written there is nothing to
        # evaluate and any state already stored would only go stale.
        self.manager.update_alerts()

        self.mock_compute.assert_not_called()
        self.assertEqual(InstanceHealthAlertsState.objects.count(), 0)

    def test_the_first_pass_records_every_alert_as_inactive(self):
        with self.assertLogs(MODULE, level="INFO"):
            self.manager.update_alerts()

        self.assertEqual(self._stored_state(), _state_with([]))
        # Initialising state is not a transition, so there is nothing to report.
        self.mock_notify.assert_called_once_with([], [])

    def test_an_alert_that_starts_firing_is_recorded_and_reported(self):
        self._seed_state(_state_with([]))
        self._set_firing(WORKERS_DOWN)

        with self.assertLogs(MODULE, level="INFO"):
            self.manager.update_alerts()

        self.assertEqual(self._stored_state(), _state_with([WORKERS_DOWN]))
        self.mock_notify.assert_called_once_with([WORKERS_DOWN], [])

    def test_an_alert_that_stops_firing_is_cleared_and_reported(self):
        self._seed_state(_state_with([WORKERS_DOWN]))

        with self.assertLogs(MODULE, level="INFO"):
            self.manager.update_alerts()

        self.assertEqual(self._stored_state(), _state_with([]))
        self.mock_notify.assert_called_once_with([], [WORKERS_DOWN])

    def test_only_the_alerts_that_changed_are_reported_as_transitions(self):
        self._seed_state(_state_with([WORKERS_DOWN]))
        self._set_firing(WORKERS_DOWN, CONNECTIONS)

        with self.assertLogs(MODULE, level="INFO"):
            self.manager.update_alerts()

        self.mock_notify.assert_called_once_with([CONNECTIONS], [])

    def test_an_alert_that_keeps_firing_is_not_reported_again(self):
        self._seed_state(_state_with([WORKERS_DOWN]))
        self._set_firing(WORKERS_DOWN)

        self.manager.update_alerts()

        self.mock_notify.assert_not_called()

    def test_a_pass_that_changes_nothing_does_not_write(self):
        # This runs every scheduler cycle, so an unconditional write would be a
        # steady stream of pointless updates to the singleton row.
        self._seed_state(_state_with([]))

        with patch.object(InstanceHealthAlertsState, "save") as mock_save:
            self.manager.update_alerts()

        mock_save.assert_not_called()

    def test_fields_stored_alongside_an_alerts_firing_state_are_preserved(self):
        self._seed_state({WORKERS_DOWN.value: {"active": False, "acknowledged_by": "someone"}})
        self._set_firing(WORKERS_DOWN)

        with self.assertLogs(MODULE, level="INFO"):
            self.manager.update_alerts()

        self.assertEqual(self._stored_state()[WORKERS_DOWN.value], {"active": True, "acknowledged_by": "someone"})

    def test_state_left_behind_by_an_alert_that_no_longer_exists_is_dropped(self):
        self._seed_state({**_state_with([]), "an_alert_that_was_removed": {"active": True}})

        with self.assertLogs(MODULE, level="INFO"):
            self.manager.update_alerts()

        self.assertEqual(self._stored_state(), _state_with([]))

    def test_the_transition_names_both_sides_in_the_log(self):
        self._seed_state(_state_with([CONNECTIONS]))
        self._set_firing(WORKERS_DOWN)

        with self.assertLogs(MODULE, level="INFO") as logs:
            self.manager.update_alerts()

        self.assertIn(WORKERS_DOWN.value, logs.output[0])
        self.assertIn(CONNECTIONS.value, logs.output[0])

    def test_a_failure_is_logged_rather_than_raised_at_the_scheduler(self):
        # This runs inside the scheduler loop, where an exception would skip the rest
        # of that cycle's work: scheduled bots, calendar syncs and the rest.
        self.mock_compute.side_effect = Exception("canceling statement due to statement timeout")

        with self.assertLogs(MODULE, level="ERROR"):
            self.manager.update_alerts()

    def test_the_new_state_is_stored_before_slack_is_told(self):
        # State written after a failed notification would be re-detected as a fresh
        # transition on the next pass, and alerted over and over.
        self._seed_state(_state_with([]))
        self._set_firing(WORKERS_DOWN)
        self.mock_notify.side_effect = Exception("slack is down")

        with self.assertLogs(MODULE, level="ERROR"):
            self.manager.update_alerts()

        self.assertEqual(self._stored_state(), _state_with([WORKERS_DOWN]))

    def test_the_singleton_row_is_created_when_it_is_missing(self):
        self.assertEqual(InstanceHealthAlertsState.objects.count(), 0)

        with self.assertLogs(MODULE, level="INFO"):
            self.manager.update_alerts()

        self.assertEqual(InstanceHealthAlertsState.objects.count(), 1)


class NotifySlackOfAlertChangesTestCase(TestCase):
    def _configure_webhook(self, url):
        """Point the notifier at a webhook, or with None, at nothing."""
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        if url is None:
            os.environ.pop(SLACK_WEBHOOK_ENV_VAR, None)
        else:
            os.environ[SLACK_WEBHOOK_ENV_VAR] = url

    def _post_and_return_mock(self, newly_active, newly_cleared, **post_kwargs):
        with patch(f"{MODULE}.requests.post", **post_kwargs) as mock_post:
            _notify_slack_of_alert_changes(newly_active, newly_cleared)
        return mock_post

    def test_nothing_is_sent_when_no_webhook_is_configured(self):
        self._configure_webhook(None)

        self.assertEqual(self._post_and_return_mock([WORKERS_DOWN], []).call_count, 0)

    def test_nothing_is_sent_when_the_webhook_is_configured_as_empty(self):
        self._configure_webhook("")

        self.assertEqual(self._post_and_return_mock([WORKERS_DOWN], []).call_count, 0)

    def test_nothing_is_sent_when_no_alert_changed(self):
        self._configure_webhook("https://hooks.slack.test/services/xyz")

        self.assertEqual(self._post_and_return_mock([], []).call_count, 0)

    def test_one_message_covers_every_change(self):
        self._configure_webhook("https://hooks.slack.test/services/xyz")

        mock_post = self._post_and_return_mock([CONNECTIONS, WORKERS_DOWN], [QUEUE_NOT_DRAINING])

        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.args[0], "https://hooks.slack.test/services/xyz")

    def test_the_message_separates_what_started_firing_from_what_stopped(self):
        self._configure_webhook("https://hooks.slack.test/services/xyz")

        mock_post = self._post_and_return_mock([WORKERS_DOWN], [CONNECTIONS])

        firing_section, cleared_section = mock_post.call_args.kwargs["json"]["text"].split("No longer firing")
        self.assertIn(ALERT_METADATA[WORKERS_DOWN]["label"], firing_section)
        self.assertNotIn(ALERT_METADATA[CONNECTIONS]["label"], firing_section)
        self.assertIn(ALERT_METADATA[CONNECTIONS]["label"], cleared_section)

    def test_a_message_with_nothing_cleared_has_no_cleared_section(self):
        self._configure_webhook("https://hooks.slack.test/services/xyz")

        mock_post = self._post_and_return_mock([WORKERS_DOWN], [])

        self.assertNotIn("No longer firing", mock_post.call_args.kwargs["json"]["text"])

    def test_the_post_is_bounded_so_it_cannot_stall_the_scheduler_loop(self):
        self._configure_webhook("https://hooks.slack.test/services/xyz")

        mock_post = self._post_and_return_mock([WORKERS_DOWN], [])

        self.assertLessEqual(mock_post.call_args.kwargs["timeout"], 10)

    def test_an_unreachable_webhook_is_logged_rather_than_raised_at_the_caller(self):
        self._configure_webhook("https://hooks.slack.test/services/xyz")

        with patch(f"{MODULE}.requests.post", side_effect=Exception("connection reset by peer")):
            with self.assertLogs(MODULE, level="WARNING"):
                _notify_slack_of_alert_changes([WORKERS_DOWN], [])

    def test_a_rejected_message_is_logged_rather_than_raised_at_the_caller(self):
        self._configure_webhook("https://hooks.slack.test/services/xyz")
        response = MagicMock()
        response.raise_for_status.side_effect = Exception("404 Client Error")

        with patch(f"{MODULE}.requests.post", return_value=response):
            with self.assertLogs(MODULE, level="WARNING"):
                _notify_slack_of_alert_changes([WORKERS_DOWN], [])
