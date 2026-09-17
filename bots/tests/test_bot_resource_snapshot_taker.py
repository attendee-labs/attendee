"""Tests for bots.bot_controller.bot_resource_snapshot_taker.

The taker measures CPU as a delta between two reads of the cgroup counter, so what it
reports depends entirely on how far apart those two reads happen. It is polled from the
bot's GLib main loop, which is not a reliable metronome: during the join phase the main
loop issues chromedriver commands that queue behind the join thread's page waits, which
can stall it for well over a minute at a stretch.

These tests pin down the tick that arrives after such a stall. Both of the taker's gates
count from the last snapshot and neither has an upper bound, so that one tick clears
both, and before the fix it opened the sampling window and closed it against the same
timestamp.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from bots.bot_controller.bot_resource_snapshot_taker import BotResourceSnapshotTaker
from bots.models import Bot, BotResourceSnapshot, Organization, Project

MODULE = "bots.bot_controller.bot_resource_snapshot_taker"

# The taker's own thresholds, restated so a change to either one fails here loudly.
SNAPSHOT_INTERVAL_SECONDS = 60
FIRST_SAMPLE_AFTER_SECONDS = 30

# The rate the fake CPU counter climbs at, which is also the millicore figure a window
# measured over its full span reports back.
CPU_COUNTER_RATE_PER_SECOND = 1000


@override_settings(SAVE_BOT_RESOURCE_SNAPSHOTS=True)
class BotResourceSnapshotTakerTests(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Org")
        project = Project.objects.create(name="Test Project", organization=organization)
        self.bot = Bot.objects.create(project=project, name="Test Bot", meeting_url="https://meet.google.com/abc-defg-hij")

        # Everything the taker reads out of /proc and /sys, so the tests turn on its
        # clock arithmetic rather than on the host they run on.
        self.cpu_counter = 0
        patches = {
            "get_cpu_usage_millicores": lambda: self.cpu_counter,
            "container_memory_mib": lambda: 207,
            "get_network_interface_stats": lambda: dict.fromkeys(["rx_bytes", "rx_packets", "rx_dropped", "rx_errors", "tx_bytes", "tx_packets", "tx_dropped", "tx_errors"], 0),
            "get_process_memory_list": lambda: [],
            "get_db_connection_count": lambda: 0,
            "get_redis_connection_count": lambda: 0,
            "get_public_ip": lambda: "127.0.0.1",
        }
        for name, replacement in patches.items():
            patcher = patch(f"{MODULE}.{name}", side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.taker = BotResourceSnapshotTaker(self.bot)

    def _advance(self, seconds):
        """Move every clock the taker is holding back by *seconds*.

        Rewinding the times it recorded is the same as waiting, without the wait or a
        patched clock. The CPU counter moves forward by the same span, as the real
        counter would.
        """
        self.cpu_counter += int(seconds * CPU_COUNTER_RATE_PER_SECOND)
        self.taker._last_snapshot_time -= timedelta(seconds=seconds)
        if self.taker._first_cpu_usage_sample_time is not None:
            self.taker._first_cpu_usage_sample_time -= timedelta(seconds=seconds)
        if self.taker._first_network_sample_time is not None:
            self.taker._first_network_sample_time -= timedelta(seconds=seconds)

    def _latest_snapshot_data(self):
        return BotResourceSnapshot.objects.order_by("-id").first().data

    def test_nothing_is_written_until_the_first_interval_has_elapsed(self):
        self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 0)

    def test_a_steady_poller_writes_one_snapshot_per_interval(self):
        # The sampling window opens partway through the interval, then the snapshot
        # closes it. Two separate calls, half a minute apart.
        self._advance(FIRST_SAMPLE_AFTER_SECONDS + 1)
        self.taker.save_snapshot_if_needed()
        self.assertEqual(BotResourceSnapshot.objects.count(), 0)

        self._advance(SNAPSHOT_INTERVAL_SECONDS - FIRST_SAMPLE_AFTER_SECONDS)
        self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 1)
        self.assertAlmostEqual(self._latest_snapshot_data()["cpu_usage_millicores"], CPU_COUNTER_RATE_PER_SECOND, delta=1)

    def test_a_tick_arriving_after_a_stall_does_not_divide_by_a_zero_second_window(self):
        # A minute and a half with no poll, which is the shape of a main loop stall
        # during the join phase. Both gates come due on this one tick: without the
        # early return it opens the sampling window and closes it against the same
        # `now`, and the CPU delta is divided by zero.
        self._advance(SNAPSHOT_INTERVAL_SECONDS + FIRST_SAMPLE_AFTER_SECONDS)

        with self.assertNoLogs(MODULE, level="ERROR"):
            self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 0)
        self.assertIsNotNone(self.taker._first_cpu_usage_sample_time)

    def test_the_snapshot_after_a_stall_is_written_on_the_following_tick(self):
        self._advance(SNAPSHOT_INTERVAL_SECONDS + FIRST_SAMPLE_AFTER_SECONDS)
        self.taker.save_snapshot_if_needed()

        # The very next tick, ~100ms later, which is all the fix waits for.
        with self.assertNoLogs(MODULE, level="ERROR"):
            self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 1)

        # Known cost of the narrow fix: that window is a fraction of a second wide, so
        # the CPU figure on this one snapshot rounds to nothing. It is a poor reading
        # rather than an exception, and the next interval recovers.
        self.assertEqual(self._latest_snapshot_data()["cpu_usage_millicores"], 0)

    def test_the_interval_returns_to_normal_after_a_stall(self):
        self._advance(SNAPSHOT_INTERVAL_SECONDS + FIRST_SAMPLE_AFTER_SECONDS)
        self.taker.save_snapshot_if_needed()
        self.taker.save_snapshot_if_needed()

        self._advance(FIRST_SAMPLE_AFTER_SECONDS + 1)
        self.taker.save_snapshot_if_needed()
        self._advance(SNAPSHOT_INTERVAL_SECONDS - FIRST_SAMPLE_AFTER_SECONDS)
        with self.assertNoLogs(MODULE, level="ERROR"):
            self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 2)
        self.assertAlmostEqual(self._latest_snapshot_data()["cpu_usage_millicores"], CPU_COUNTER_RATE_PER_SECOND, delta=1)

    def test_repeated_stalls_keep_producing_snapshots(self):
        for expected_count in (1, 2, 3):
            self._advance(SNAPSHOT_INTERVAL_SECONDS + FIRST_SAMPLE_AFTER_SECONDS)
            self.taker.save_snapshot_if_needed()
            self.taker.save_snapshot_if_needed()

            self.assertEqual(BotResourceSnapshot.objects.count(), expected_count)
