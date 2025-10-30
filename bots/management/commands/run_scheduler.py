import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import connection, models, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Organization
from bots.models import Bot, BotStates, Calendar, CalendarStates, ZoomOAuthConnection, ZoomOAuthConnectionStates
from bots.tasks.autopay_charge_task import enqueue_autopay_charge_task
from bots.tasks.launch_scheduled_bot_task import launch_scheduled_bot
from bots.tasks.sync_calendar_task import enqueue_sync_calendar_task
from bots.tasks.sync_zoom_oauth_connection_task import enqueue_sync_zoom_oauth_connection_task

log = logging.getLogger(__name__)

CALENDAR_SYNC_THRESHOLD_HOURS = 24  # The longest a calendar can go without having been synced


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
                self._run_periodic_calendar_syncs()
                self._run_periodic_zoom_oauth_connection_syncs()
                self._run_autopay_tasks()
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

    def _run_periodic_calendar_syncs(self):
        """
        Run periodic calendar syncs.
        Launch sync tasks for calendars that haven't had a sync task enqueued in the last 24 hours.
        """
        now = timezone.now()
        cutoff_time = now - timezone.timedelta(hours=CALENDAR_SYNC_THRESHOLD_HOURS)

        # Find connected calendars that haven't had a sync task enqueued in the last 24 hours
        calendars = Calendar.objects.filter(
            state=CalendarStates.CONNECTED,
        ).filter(Q(sync_task_enqueued_at__isnull=True) | Q(sync_task_enqueued_at__lte=cutoff_time) | Q(sync_task_requested_at__isnull=False))

        for calendar in calendars:
            last_enqueued = calendar.sync_task_enqueued_at.isoformat() if calendar.sync_task_enqueued_at else "never"
            log.info("Launching calendar sync for calendar %s (last enqueued: %s)", calendar.object_id, last_enqueued)
            enqueue_sync_calendar_task(calendar)

        log.info("Launched %d calendar sync tasks", len(calendars))

    def _run_periodic_zoom_oauth_connection_syncs(self):
        """
        Run periodic zoom oauth connection syncs.
        Launch sync tasks for zoom oauth connections that haven't had a sync task enqueued in the last 7 days.
        """
        now = timezone.now()
        cutoff_time = now - timezone.timedelta(days=7)

        # Find connected zoom oauth connections that haven't had a sync task enqueued in the last 7 days
        zoom_oauth_connections = ZoomOAuthConnection.objects.filter(
            state=ZoomOAuthConnectionStates.CONNECTED,
        ).filter(Q(sync_task_enqueued_at__isnull=True) | Q(sync_task_enqueued_at__lte=cutoff_time) | Q(sync_task_requested_at__isnull=False))

        for zoom_oauth_connection in zoom_oauth_connections:
            last_enqueued = zoom_oauth_connection.sync_task_enqueued_at.isoformat() if zoom_oauth_connection.sync_task_enqueued_at else "never"
            log.info("Launching zoom oauth connection sync for zoom oauth connection %s (last enqueued: %s)", zoom_oauth_connection.object_id, last_enqueued)
            enqueue_sync_zoom_oauth_connection_task(zoom_oauth_connection)

        log.info("Launched %d zoom oauth connection sync tasks", len(zoom_oauth_connections))

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

    def _run_autopay_tasks(self):
        """
        Run autopay tasks for organizations that meet all criteria:
        - Autopay is enabled
        - Has a Stripe customer ID
        - Credit balance is below the threshold
        - No autopay task has been enqueued in the last day
        """
        now = timezone.now()
        cutoff_time = now - timezone.timedelta(days=1)

        # Find organizations that meet all autopay criteria
        organizations = Organization.objects.filter(
            # Autopay must be enabled
            autopay_enabled=True,
            # Must have a Stripe customer ID
            autopay_stripe_customer_id__isnull=False,
            # Credit balance must be below threshold
            centicredits__lt=models.F("autopay_threshold_centricredits"),
            # No charge failure
            autopay_charge_failure_data__isnull=True,
        ).filter(
            # No autopay task enqueued in the last day (or never enqueued)
            Q(autopay_charge_task_enqueued_at__isnull=True) | Q(autopay_charge_task_enqueued_at__lte=cutoff_time)
        )

        for organization in organizations:
            credits = organization.credits()
            threshold = organization.autopay_threshold_credits()
            last_enqueued = organization.autopay_charge_task_enqueued_at.isoformat() if organization.autopay_charge_task_enqueued_at else "never"

            log.info(
                "Enqueueing autopay task for organization %s (credits: %.2f, threshold: %.2f, last enqueued: %s)",
                organization.id,
                credits,
                threshold,
                last_enqueued,
            )

            enqueue_autopay_charge_task(organization)

        log.info("Enqueued %d autopay tasks", len(organizations))
