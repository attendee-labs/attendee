"""Tests for bots.bot_controller.bot_resource_snapshot_taker.

The taker is polled from the bot's GLib main loop, so it owns its own clocks and
has to stay correct no matter how irregularly it is called. These tests drive it
through the poll patterns that actually happen in production: the steady 100ms
tick, and the tick that arrives late because the main loop was blocked.
"""

import datetime
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization
from bots.bot_controller.bot_resource_snapshot_taker import (
    BotResourceSnapshotTaker,
    pod_cpu_millicores,
)
from bots.models import Bot, BotResourceSnapshot, Project

MODULE = "bots.bot_controller.bot_resource_snapshot_taker"


@override_settings(SAVE_BOT_RESOURCE_SNAPSHOTS=True)
class BotResourceSnapshotTakerTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            name="Test Bot",
            project=self.project,
            meeting_url="https://zoom.us/j/test",
        )

        # The counter climbs by 100 millicore-seconds per wall clock second, so any
        # correctly measured window reports 100 millicores.
        self.cpu_counter = 0

        patches = [
            patch(f"{MODULE}.get_cpu_usage_millicores", side_effect=lambda: self.cpu_counter),
            patch(f"{MODULE}.container_memory_mib", return_value=512),
            patch(f"{MODULE}.get_process_memory_list", return_value=[]),
            patch(f"{MODULE}.get_db_connection_count", return_value=1),
            patch(f"{MODULE}.get_redis_connection_count", return_value=1),
            patch(f"{MODULE}.get_network_interface_stats", return_value=dict.fromkeys(["rx_bytes", "rx_packets", "rx_dropped", "rx_errors", "tx_bytes", "tx_packets", "tx_dropped", "tx_errors"], 0)),
            # The taker spawns a thread to fetch the public IP on construction.
            patch(f"{MODULE}.get_public_ip", return_value="203.0.113.1"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.start_time = timezone.now()

    def poll_at(self, taker, seconds_since_start):
        """Poll the taker as the main loop would, at a given offset from start."""
        now = self.start_time + datetime.timedelta(seconds=seconds_since_start)
        self.cpu_counter = int(100 * seconds_since_start)
        with patch(f"{MODULE}.timezone.now", return_value=now):
            taker.save_snapshot_if_needed()

    def poll_from_to(self, taker, start_seconds, end_seconds, step_seconds=0.5):
        """Poll the taker on a steady tick across a span of time."""
        offset = start_seconds
        while offset <= end_seconds:
            self.poll_at(taker, offset)
            offset = round(offset + step_seconds, 3)

    def make_taker(self):
        with patch(f"{MODULE}.timezone.now", return_value=self.start_time):
            return BotResourceSnapshotTaker(self.bot)

    def test_records_cpu_usage_on_a_steady_poll(self):
        taker = self.make_taker()

        self.poll_from_to(taker, 0.5, 70)

        snapshot = BotResourceSnapshot.objects.get(bot=self.bot)
        self.assertEqual(snapshot.data["cpu_usage_millicores"], 100)
        self.assertEqual(snapshot.data["ram_usage_megabytes"], 512)

    def test_poll_arriving_after_the_snapshot_is_due_does_not_divide_by_zero(self):
        """A tick that lands past the snapshot interval must not sample CPU twice.

        The main loop can block for longer than the snapshot interval (a hung page
        load, a slow adapter callback), so the next poll arrives with both the CPU
        sampling window and the snapshot interval already elapsed. Taking the first
        and second CPU samples in that one poll leaves a zero length window.
        """
        taker = self.make_taker()

        with self.assertNoLogs(MODULE, level="ERROR"):
            self.poll_at(taker, 61)

    def test_recovers_after_a_blocked_main_loop(self):
        """The poll after a late one still produces a snapshot with real CPU usage."""
        taker = self.make_taker()

        self.poll_at(taker, 61)
        self.poll_from_to(taker, 61.5, 135)

        snapshot = BotResourceSnapshot.objects.filter(bot=self.bot).latest("created_at")
        self.assertEqual(snapshot.data["cpu_usage_millicores"], 100)

    def test_pod_cpu_millicores_rejects_a_non_positive_window(self):
        with self.assertRaises(ValueError):
            pod_cpu_millicores(0, 0, 100)
