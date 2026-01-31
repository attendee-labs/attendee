import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import connection, models
from django.utils import timezone
from kubernetes import client, config

from bots.bot_pod_creator.bot_pod_utils import locate_pod_for_bot
from bots.launch_bot_utils import launch_bot
from bots.models import Bot, BotEventTypes, BotStates

logger = logging.getLogger(__name__)

# For dealing with this GKE issue: https://discuss.google.dev/t/gke-autopilot-and-preemted-pods/191410/14


class Command(BaseCommand):
    help = "Runs a daemon that detects bots that failed to launch due to GKE issues and re-launches them."

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
        logger.info("Received %s, shutting down after current cycle", signum)
        self._keep_running = False

    def handle(self, *args, **opts):
        # Initialize kubernetes client
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.v1 = client.CoreV1Api()

        # Trap SIGINT / SIGTERM so Kubernetes or Heroku can stop the container cleanly
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        interval = opts["interval"]
        logger.info("Correct failed bot launches daemon started, polling every %s seconds", interval)

        while self._keep_running:
            began = time.monotonic()
            try:
                self._correct_failed_bot_launches()
            except Exception:
                logger.exception("Correct failed bot launches cycle failed")
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
                logger.warning(f"Correct failed bot launches cycle took {elapsed}s, which is longer than the interval of {interval}s")

        logger.info("Correct failed bot launches daemon exited")

    def bot_pod_is_active(self, bot) -> bool:
        pod_info = locate_pod_for_bot(self.v1, bot)
        if not pod_info:
            return False

        pod_name = pod_info.pod_name
        try:
            logger.info(f"Checking if pod {pod_name} is active...")
            pod = self.v1.read_namespaced_pod(name=pod_name, namespace=pod_info.namespace)
            # Log all the info about the pod
            logger.info(f"Pod {pod_name} phase: {pod.status.phase}")
            # Return that it is active if pod is not in succeeded or failed phase
            if pod.status.phase not in ["Succeeded", "Failed"]:
                return True
            # Otherwise it is in one of these phases, but it needs to be deleted
            self.v1.delete_namespaced_pod(name=pod_name, namespace=pod_info.namespace, grace_period_seconds=5)
            logger.info(f"Deleted pod so that it can be re-launched: {pod_name}")
            return False
        except client.ApiException as e:
            if e.status == 404:
                return False
            raise

    def _correct_failed_bot_launches(self):
        logger.info("Looking for bots created in last 5 minutes that failed to launch...")

        try:
            # Calculate timestamps within 5 minutes ago
            five_minutes_ago = timezone.now() - timezone.timedelta(minutes=5)
            one_minute_ago = timezone.now() - timezone.timedelta(minutes=1)

            # Find non-post-meeting bots where:
            # - created between 5 minutes and 1 minute ago AND join_at is null
            # - first heartbeat is null (bot pod never ran)
            # - state is joining
            failed_to_launch_non_scheduled_q_filter = models.Q(created_at__gt=five_minutes_ago, created_at__lt=one_minute_ago, first_heartbeat_timestamp__isnull=True, join_at__isnull=True)
            problem_non_scheduled_bots = Bot.objects.filter(failed_to_launch_non_scheduled_q_filter).filter(state=BotStates.JOINING)

            failed_to_launch_scheduled_q_filter = models.Q(join_at__gt=five_minutes_ago, join_at__lt=one_minute_ago, first_heartbeat_timestamp__isnull=True, join_at__isnull=False)
            problem_scheduled_bots = Bot.objects.filter(failed_to_launch_scheduled_q_filter).filter(state=BotStates.STAGED)

            logger.info(f"Found {problem_non_scheduled_bots.count()} non-scheduled and {problem_scheduled_bots.count()} scheduled bots that failed to launch")

            # Re-launch each bot
            for bot in problem_non_scheduled_bots:
                try:
                    if self.bot_pod_is_active(bot):
                        logger.info(f"Bot {bot.object_id} already has a pod, skipping re-launch")
                        continue
                    if bot.should_launch_webpage_streamer():
                        logger.info(f"Bot {bot.object_id} should launch a webpage streamer, skipping re-launch")
                        continue
                    last_bot_event = bot.last_bot_event()
                    if last_bot_event.event_type != BotEventTypes.JOIN_REQUESTED:
                        logger.info(f"Bot {bot.object_id} is not in JOINING state, skipping re-launch")
                        continue
                    if last_bot_event.requested_bot_action_taken_at is not None:
                        logger.info(f"Bot {bot.object_id} has already had a bot action taken, skipping re-launch")
                        continue
                    logger.info(f"Re-launching non-scheduled bot {bot.object_id} that failed to launch")
                    launch_bot(bot)
                except Exception as e:
                    logger.error(f"Failed to re-launch non-scheduled bot {bot.object_id}: {str(e)}")

            for bot in problem_scheduled_bots:
                try:
                    if self.bot_pod_is_active(bot):
                        logger.info(f"Bot {bot.object_id} already has a pod, skipping re-launch")
                        continue
                    if bot.should_launch_webpage_streamer():
                        logger.info(f"Bot {bot.object_id} should launch a webpage streamer, skipping re-launch")
                        continue
                    last_bot_event = bot.last_bot_event()
                    if last_bot_event.event_type != BotEventTypes.STAGED:
                        logger.info(f"Bot {bot.object_id} is not in STAGED state, skipping re-launch")
                        continue
                    logger.info(f"Re-launching scheduled bot {bot.object_id} that failed to launch")
                    launch_bot(bot)
                except Exception as e:
                    logger.error(f"Failed to re-launch scheduled bot {bot.object_id}: {str(e)}")

            logger.info("Finished re-launching bots that failed to launch")

        except Exception as e:
            logger.error(f"Failed to re-launch bots that failed to launch: {str(e)}")
