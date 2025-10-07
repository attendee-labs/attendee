import logging
import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import models
from django.utils import timezone
from kubernetes import client, config

from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Terminates bots that have not sent a heartbeat in the last ten minutes or that never launched"

    def __init__(self):
        super().__init__()
        self.namespace = settings.BOT_POD_NAMESPACE

    def terminate_bot(self, bot, event_sub_type):
        try:
            BotEventManager.create_event(
                bot=bot,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=event_sub_type,
            )
        except Exception as e:
            logger.error(f"Failed to create fatal error {event_sub_type} event for bot {bot.id}: {str(e)}")

        # There isn't really a safe way to terminate the bot if it's running as a celery task
        if not os.getenv("LAUNCH_BOT_METHOD") == "kubernetes":
            return

        # Initialize kubernetes client
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        v1 = client.CoreV1Api()
        logger.info("initialized kubernetes client")

        # Try to delete the pod if it exists
        try:
            pod_name = bot.k8s_pod_name()
            v1.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                grace_period_seconds=0,
            )
            logger.info(f"Deleted pod: {pod_name}")
        except client.ApiException as pod_error:
            # 404 means pod doesn't exist, which is fine
            if pod_error.status != 404:
                logger.warning(f"Error deleting pod {pod_name}: {str(pod_error)}")

    def handle(self, *args, **options):
        self.terminate_bots_with_heartbeat_timeout()
        self.terminate_bots_that_never_launched()

    def terminate_bots_with_heartbeat_timeout(self):
        logger.info("Terminating bots with heartbeat timeout...")

        try:
            ten_minutes_ago_timestamp = int(timezone.now().timestamp() - 600)

            # Find non post-meeting bots where the last heartbeat is over 10 minutes ago
            heartbeat_timeout_q_filter = models.Q(last_heartbeat_timestamp__isnull=False) & models.Q(last_heartbeat_timestamp__lt=ten_minutes_ago_timestamp)
            problem_bots = Bot.objects.filter(~BotEventManager.get_post_meeting_states_q_filter() & heartbeat_timeout_q_filter)

            logger.info(f"Found {problem_bots.count()} bots with heartbeat timeout")

            # Create fatal error events for each bot
            for bot in problem_bots:
                try:
                    logger.info(f"Terminating bot {bot.object_id} due to heartbeat timeout")
                    self.terminate_bot(bot, BotEventSubTypes.FATAL_ERROR_HEARTBEAT_TIMEOUT)

                except Exception as e:
                    logger.error(f"Failed to terminate bot {bot.object_id}: {str(e)}")

            logger.info("Finished terminating bots with heartbeat timeout")

        except client.ApiException as e:
            logger.error(f"Failed to terminate bots with heartbeat timeout: {str(e)}")

    def terminate_bots_that_never_launched(self):
        logger.info("Terminating bots that never launched...")

        try:
            # Calculate timestamps for 7 days ago and 1 hour ago
            seven_days_ago = timezone.now() - timezone.timedelta(days=7)
            one_hour_ago = timezone.now() - timezone.timedelta(hours=1)

            # Find non-post-meeting bots where:
            # - created between 7 days and 1 hour ago AND join_at is null OR join_at is between 7 days and 1 hour ago
            # - first heartbeat is null (never launched)
            never_launched_q_filter = models.Q(created_at__gt=seven_days_ago, created_at__lt=one_hour_ago, first_heartbeat_timestamp__isnull=True, join_at__isnull=True) | models.Q(join_at__gt=seven_days_ago, join_at__lt=one_hour_ago, first_heartbeat_timestamp__isnull=True)
            problem_bots = Bot.objects.filter(~BotEventManager.get_post_meeting_states_q_filter() & never_launched_q_filter)

            logger.info(f"Found {problem_bots.count()} bots that never launched")

            # Create fatal error events for each bot
            for bot in problem_bots:
                try:
                    logger.info(f"Terminating bot {bot.object_id} that never launched")
                    self.terminate_bot(bot, BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED)

                except Exception as e:
                    logger.error(f"Failed to terminate bot {bot.object_id}: {str(e)}")

            logger.info("Finished terminating bots that never launched")

        except Exception as e:
            logger.error(f"Failed to terminate bots that never launched: {str(e)}")
