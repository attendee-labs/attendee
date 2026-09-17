"""Tests for bots.bot_controller.bot_resource_snapshot_taker.

The taker reports CPU and network as deltas against the previous reading, so what it
reports depends on how far apart two readings are. It is polled from the bot's GLib main
loop, which is not a reliable metronome: during the join phase the main loop issues
chromedriver commands that queue behind the join thread's page waits, which can stall it
for well over a minute at a stretch. These tests cover the tick that arrives after such
a stall.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from bots.bot_controller.bot_resource_snapshot_taker import BotResourceSnapshotTaker
from bots.models import Bot, BotResourceSnapshot, Organization, Project

MODULE = "bots.bot_controller.bot_resource_snapshot_taker"

SNAPSHOT_INTERVAL_SECONDS = 60

# The rate the fake CPU counter climbs at, which is also the millicore figure a
# correctly measured interval reports back.
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
            "get_public_ip": lambda: "203.0.113.1",
        }
        for name, replacement in patches.items():
            patcher = patch(f"{MODULE}.{name}", side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.taker = BotResourceSnapshotTaker(self.bot)

    def _advance(self, seconds):
        """Move the times the taker recorded back by *seconds*, and the counter forward."""
        self.cpu_counter += int(seconds * CPU_COUNTER_RATE_PER_SECOND)
        self.taker._last_snapshot_time -= timedelta(seconds=seconds)
        self.taker._last_reading_time -= timedelta(seconds=seconds)

    def _latest_snapshot_data(self):
        return BotResourceSnapshot.objects.order_by("-id").first().data

    def test_nothing_is_written_until_the_first_interval_has_elapsed(self):
        self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 0)

    def test_each_interval_writes_a_snapshot_measured_over_that_whole_interval(self):
        for expected_count in (1, 2, 3):
            self._advance(SNAPSHOT_INTERVAL_SECONDS)
            self.taker.save_snapshot_if_needed()

            self.assertEqual(BotResourceSnapshot.objects.count(), expected_count)
            self.assertAlmostEqual(self._latest_snapshot_data()["cpu_usage_millicores"], CPU_COUNTER_RATE_PER_SECOND, delta=1)

    def test_a_tick_arriving_after_a_stall_measures_over_the_real_gap(self):
        # A minute and a half with no poll, which is the shape of a real main loop stall
        # during the join phase. The previous reading is that much older, and the delta
        # has to be divided by the gap that actually elapsed.
        self._advance(SNAPSHOT_INTERVAL_SECONDS + 30)

        with self.assertNoLogs(MODULE, level="ERROR"):
            self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 1)
        self.assertAlmostEqual(self._latest_snapshot_data()["cpu_usage_millicores"], CPU_COUNTER_RATE_PER_SECOND, delta=1)
        self.assertIsNotNone(self._latest_snapshot_data()["network"])

    def test_a_stall_does_not_disturb_the_snapshots_that_follow_it(self):
        self._advance(SNAPSHOT_INTERVAL_SECONDS + 30)
        self.taker.save_snapshot_if_needed()

        self._advance(SNAPSHOT_INTERVAL_SECONDS)
        with self.assertNoLogs(MODULE, level="ERROR"):
            self.taker.save_snapshot_if_needed()

        self.assertEqual(BotResourceSnapshot.objects.count(), 2)
        self.assertAlmostEqual(self._latest_snapshot_data()["cpu_usage_millicores"], CPU_COUNTER_RATE_PER_SECOND, delta=1)
