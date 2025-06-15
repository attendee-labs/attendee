import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from bots.models import Bot, BotStates
from bots.tasks.launch_scheduled_bot_task import launch_scheduled_bot

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs celery tasks for scheduled bots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=60,
            help="Polling interval in seconds (default: 60)",
        )

    # Graceful shutdown flags
    _keep_running = True

    def _graceful_exit(self, signum, frame):
        log.info("Received %s, shutting down after current cycle", signum)
        self._keep_running = False

    def handle(self, *args, **opts):
        # Trap SIGINT / SIGTERM so Kubernetes or Heroku can stop the container cleanly
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        interval = opts["interval"]
        log.info("Scheduler daemon started, polling every %s seconds", interval)

        while self._keep_running:
            began = time.monotonic()
            try:
                self._run_scheduled_bots()
            except Exception:
                log.exception("Scheduler cycle failed")
            finally:
                # Close stale connections so the loop never inherits a dead socket
                connection.close()

            # Sleep the *remainder* of the interval, even if work took time T
            elapsed = time.monotonic() - began
            remaining_sleep = max(0, interval - elapsed)

            # Break sleep into smaller chunks to allow for more responsive shutdown
            sleep_chunk = 1  # Sleep 1 second at a time
            while remaining_sleep > 0 and self._keep_running:
                chunk_sleep = min(sleep_chunk, remaining_sleep)
                time.sleep(chunk_sleep)
                remaining_sleep -= chunk_sleep

            # If we took longer than the interval, we should log a warning
            if elapsed > interval:
                log.warning(f"Scheduler cycle took {elapsed}s, which is longer than the interval of {interval}s")

        log.info("Scheduler daemon exited")

    # -----------------------------------------------------------
    def _run_scheduled_bots(self):
        """
        Promote objects whose join_at ≤ join_at_threshold.
        Uses SELECT … FOR UPDATE SKIP LOCKED so multiple daemons
        can run safely (e.g. during rolling deploys).
        """

        # Give the bots 5 minutes to spin up, before they join the meeting.
        join_at_upper_threshold = timezone.now() + timezone.timedelta(minutes=5)
        # If we miss a scheduled bot by more than 5 minutes, don't bother launching it, it's a failure and it'll be cleaned up
        # by the clean_up_bots_with_heartbeat_timeout_or_that_never_launched command
        join_at_lower_threshold = timezone.now() - timezone.timedelta(minutes=5)

        with transaction.atomic():
            bots_to_launch = Bot.objects.filter(state=BotStates.SCHEDULED, join_at__lte=join_at_upper_threshold, join_at__gte=join_at_lower_threshold).select_for_update(skip_locked=True)

            for bot in bots_to_launch:
                log.info(f"Launching scheduled bot {bot.id} ({bot.object_id}) with join_at {bot.join_at.isoformat()}")
                launch_scheduled_bot.delay(bot.id, bot.join_at.isoformat())

            log.info("Launched %s bots", len(bots_to_launch))
