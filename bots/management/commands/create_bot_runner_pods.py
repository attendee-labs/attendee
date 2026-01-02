import logging
import os
import signal
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from kubernetes import client, config

from bots.bot_pod_creator.bot_pod_creator import BotPodCreator

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Maintains a pool of unassigned bot runner pods"

    def __init__(self):
        super().__init__()
        # Initialize kubernetes client
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.v1 = client.CoreV1Api()
        self.namespace = settings.BOT_POD_NAMESPACE
        logger.info("Initialized kubernetes client for namespace %s", self.namespace)

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=60,
            help="Polling interval in seconds (default: 60)",
        )
        parser.add_argument(
            "--desired-count",
            type=int,
            default=None,
            help="Desired number of unassigned bot runner pods (default: from BOT_RUNNER_POOL_SIZE env var or 5)",
        )

    # Graceful shutdown flags
    _keep_running = True

    def _graceful_exit(self, signum, frame):
        logger.info("Received %s, shutting down after current cycle", signum)
        self._keep_running = False

    def handle(self, *args, **opts):
        # Trap SIGINT / SIGTERM so Kubernetes can stop the container cleanly
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        interval = opts["interval"]
        desired_count = opts["desired_count"] or int(os.getenv("BOT_RUNNER_POOL_SIZE", "5"))

        logger.info(
            "Bot runner pool manager started, polling every %s seconds, desired pool size: %s",
            interval,
            desired_count,
        )

        while self._keep_running:
            began = time.monotonic()
            try:
                self._maintain_bot_runner_pool(desired_count)
            except Exception:
                logger.exception("Bot runner pool maintenance cycle failed")
            finally:
                # Close stale connections so the loop never inherits a dead socket
                connection.close()

            # Sleep the remainder of the interval
            elapsed = time.monotonic() - began
            remaining_sleep = max(0, interval - elapsed)

            # Break sleep into smaller chunks to allow for more responsive shutdown
            sleep_chunk = 1
            while remaining_sleep > 0 and self._keep_running:
                chunk_sleep = min(sleep_chunk, remaining_sleep)
                time.sleep(chunk_sleep)
                remaining_sleep -= chunk_sleep

            if elapsed > interval:
                logger.warning(
                    "Bot runner pool maintenance cycle took %.1fs, which is longer than the interval of %ss",
                    elapsed,
                    interval,
                )

        logger.info("Bot runner pool manager exited")

    def _get_unassigned_bot_runner_pods(self):
        """
        Get all unassigned bot runner pods in the namespace.

        A pod is considered an unassigned bot runner if:
        - It has the label "is-bot-runner=true"
        - It has the annotation "assigned-bot-id" set to empty string
        - It is in a Running or Pending phase (not completed/failed)
        """
        try:
            pods = self.v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector="is-bot-runner=true",
            )

            unassigned_pods = []
            for pod in pods.items:
                # Check if the pod has an empty assigned-bot-id annotation
                annotations = pod.metadata.annotations or {}
                assigned_bot_id = annotations.get("assigned-bot-id", None)

                # Pod is unassigned if annotation exists and is empty
                if assigned_bot_id == "":
                    unassigned_pods.append(pod)

            return unassigned_pods

        except client.ApiException as e:
            logger.error("Failed to list bot runner pods: %s", e)
            return []

    def _maintain_bot_runner_pool(self, desired_count: int):
        """
        Ensure there are at least `desired_count` unassigned bot runner pods.
        Creates new pods if the current count is below the desired count.
        """
        unassigned_pods = self._get_unassigned_bot_runner_pods()
        current_count = len(unassigned_pods)

        logger.info(
            "Bot runner pool status: %s unassigned pods, desired: %s",
            current_count,
            desired_count,
        )

        if current_count >= desired_count:
            logger.debug("Pool size is sufficient, no new pods needed")
            return

        pods_to_create = desired_count - current_count
        logger.info("Creating %s new bot runner pods to reach desired pool size", pods_to_create)

        for i in range(pods_to_create):
            bot_pod_creator = BotPodCreator()
            if not self._keep_running:
                logger.info("Shutdown requested, stopping pod creation")
                break

            bot_runner_uuid = str(uuid.uuid4())
            try:
                result = bot_pod_creator.create_bot_pod(
                    bot_id=None,
                    bot_runner_uuid=bot_runner_uuid,
                )

                if result.get("created"):
                    logger.info(
                        "Created bot runner pod %s (%s/%s)",
                        result["name"],
                        i + 1,
                        pods_to_create,
                    )
                else:
                    logger.error(
                        "Failed to create bot runner pod: %s",
                        result.get("error", "Unknown error"),
                    )

            except Exception as e:
                logger.exception("Exception creating bot runner pod: %s", e)
