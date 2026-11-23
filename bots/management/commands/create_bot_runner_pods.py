import logging
import math
import os
import signal
import time
import uuid
from collections import deque

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from kubernetes import client, config

from bots.bot_pod_creator.bot_pod_creator import BotPodCreator
from bots.bot_pod_creator.bot_runner_pod_assigner import get_bot_runner_pool_launch_counts

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

        # Get the current release version to filter pods by matching version
        self.app_version = os.getenv("CUBER_RELEASE_VERSION")
        if not self.app_version:
            raise ValueError("CUBER_RELEASE_VERSION environment variable is required")

        logger.info("Initialized kubernetes client for namespace %s with version %s", self.namespace, self.app_version)

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
            help="Deprecated. Keeps the pool at a fixed size by setting both --min-size and --max-size (default: from BOT_RUNNER_POOL_SIZE env var)",
        )
        parser.add_argument(
            "--min-size",
            type=int,
            default=None,
            help="Smallest pool size, used when no bots have asked for a runner recently (default: from BOT_RUNNER_POOL_MIN_SIZE env var or 1)",
        )
        parser.add_argument(
            "--max-size",
            type=int,
            default=None,
            help="Largest pool size, also held for the first --peak-launch-lookback-seconds after startup (default: from BOT_RUNNER_POOL_MAX_SIZE env var or 5)",
        )
        parser.add_argument(
            "--peak-launch-multiplier",
            type=float,
            default=None,
            help="Pool pods kept for each bot launch in the poll cycle that had the most launches during the lookback (default: from BOT_RUNNER_POOL_PEAK_LAUNCH_MULTIPLIER env var or 2)",
        )
        parser.add_argument(
            "--peak-launch-lookback-seconds",
            type=int,
            default=None,
            help="How far back to look for the poll cycle that had the most bot launches. The pool only shrinks once a burst of launches is older than this (default: from BOT_RUNNER_POOL_PEAK_LAUNCH_LOOKBACK_SECONDS env var or 3600)",
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
        desired_count = opts["desired_count"] if opts["desired_count"] is not None else os.getenv("BOT_RUNNER_POOL_SIZE")
        self.min_size = opts["min_size"] if opts["min_size"] is not None else int(os.getenv("BOT_RUNNER_POOL_MIN_SIZE", desired_count if desired_count is not None else "1"))
        self.max_size = opts["max_size"] if opts["max_size"] is not None else int(os.getenv("BOT_RUNNER_POOL_MAX_SIZE", desired_count if desired_count is not None else "5"))
        self.peak_launch_multiplier = opts["peak_launch_multiplier"] if opts["peak_launch_multiplier"] is not None else float(os.getenv("BOT_RUNNER_POOL_PEAK_LAUNCH_MULTIPLIER", "2"))
        peak_launch_lookback_seconds = opts["peak_launch_lookback_seconds"] or int(os.getenv("BOT_RUNNER_POOL_PEAK_LAUNCH_LOOKBACK_SECONDS", "3600"))
        self._launches_per_cycle = deque(maxlen=max(1, math.ceil(peak_launch_lookback_seconds / interval)))
        self._prev_launch_counts = None
        # Without launch history the pool would start at min_size, e.g. right before a spike after a deploy
        self._hold_max_size_until = time.monotonic() + peak_launch_lookback_seconds

        logger.info(
            "Bot runner pool manager started, polling every %s seconds, pool size: min %s, max %s, peak launch multiplier %s, peak launch lookback %ss",
            interval,
            self.min_size,
            self.max_size,
            self.peak_launch_multiplier,
            peak_launch_lookback_seconds,
        )

        while self._keep_running:
            began = time.monotonic()
            try:
                self._maintain_bot_runner_pool(self._desired_pool_size())
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
        Get all unassigned bot runner pods in the namespace that match the current version.
        Returns None if the pods could not be listed.

        A pod is considered an unassigned bot runner if:
        - It has the label "is-bot-runner=true"
        - It has the label "assigned-bot-id" set to "none"
        - It is in a Running or Pending phase (not completed/failed)
        - It is not being deleted (no deletion_timestamp)
        - It has the same app.kubernetes.io/version label as this command
        """
        try:
            pods = self.v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"is-bot-runner=true,assigned-bot-id=none,app.kubernetes.io/version={self.app_version}",
            )

            return [pod for pod in pods.items if pod.status.phase in ("Running", "Pending") and pod.metadata.deletion_timestamp is None]

        except client.ApiException as e:
            logger.error("Failed to list bot runner pods: %s", e)
            return None

    def _get_stale_unassigned_bot_runner_pods(self):
        """
        Get all unassigned bot runner pods that are on a different version than the current release.

        These pods should be cleaned up during rolling deployments.
        """
        try:
            # Get all unassigned bot runner pods, then filter out current version
            pods = self.v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector="is-bot-runner=true,assigned-bot-id=none",
            )

            stale_pods = []
            for pod in pods.items:
                # Only consider pods that have a different release version
                labels = pod.metadata.labels or {}
                pod_version = labels.get("app.kubernetes.io/version")
                if pod_version != self.app_version:
                    stale_pods.append(pod)

            return stale_pods

        except client.ApiException as e:
            logger.error("Failed to list stale bot runner pods: %s", e)
            return []

    def _count_new_launches(self):
        """
        Count the bot launches that asked for a runner since the previous call, by outcome.
        Returns None if the counts could not be read.

        BotRunnerPodAssigner records these as the launches happen, so they include launches that fell
        back to a dedicated pod because the pool was empty, and runners that were claimed and are already gone.
        """
        try:
            launch_counts = get_bot_runner_pool_launch_counts()
        except redis.RedisError as e:
            logger.error("Failed to read bot runner pool launch counts: %s", e)
            return None

        new_launches = {}
        if self._prev_launch_counts is not None:
            for outcome, count in launch_counts.items():
                prev_count = self._prev_launch_counts.get(outcome, 0)
                # A total can only go down if the key was lost from Redis, in which case it restarted from zero
                new_launches[outcome] = count - prev_count if count >= prev_count else count
        self._prev_launch_counts = launch_counts
        return new_launches

    def _desired_pool_size(self) -> int:
        """
        Size the pool from the poll cycle that had the most launches during the lookback. A claimed runner
        takes about a poll interval plus pod startup time to be replaced, so the pool keeps
        peak_launch_multiplier pods for each launch in that cycle to cover the launches that arrive in the meantime.
        Using the peak grows the pool on the cycle after a burst, and only shrinks it once the burst
        is older than the lookback.

        The pool is held at max_size on any cycle where the launch counts could not be read.
        """
        new_launches = self._count_new_launches()
        if new_launches is not None:
            self._launches_per_cycle.append(sum(new_launches.values()))

        peak_launches_per_cycle = max(self._launches_per_cycle, default=0)
        if new_launches is None:
            # Launches can't be seen right now, so the lookback may be missing a burst
            target = self.max_size
        elif time.monotonic() < self._hold_max_size_until:
            target = self.max_size
        else:
            target = math.ceil(peak_launches_per_cycle * self.peak_launch_multiplier)
        desired_count = max(self.min_size, min(self.max_size, target))

        logger.info(
            "Bot runner pool sizing: new launches this cycle %s, peak launches per cycle in lookback %s, desired pool size %s, launches per cycle in lookback (oldest first): %s",
            new_launches,
            peak_launches_per_cycle,
            desired_count,
            ",".join(map(str, self._launches_per_cycle)),
        )
        return desired_count

    def _delete_pod_if_unchanged(self, pod) -> bool:
        pod_name = pod.metadata.name
        opts = client.V1DeleteOptions(
            preconditions=client.V1Preconditions(
                uid=pod.metadata.uid,
                resource_version=pod.metadata.resource_version,
            )
        )
        try:
            self.v1.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=opts,
            )
            logger.info("Deleted pod %s (rv=%s)", pod_name, pod.metadata.resource_version)
            return True
        except client.ApiException as e:
            # 409 Conflict is typical for failed preconditions
            if e.status == 409:
                logger.info("Skip deleting %s: pod changed since list (precondition failed)", pod_name)
                return False
            if e.status == 404:
                logger.info("Pod %s already gone", pod_name)
                return False
            logger.error("Failed to delete pod %s: %s", pod_name, e)
            return False

    def _maintain_bot_runner_pool(self, desired_count: int):
        """
        Ensure there are exactly `desired_count` unassigned bot runner pods.
        Creates new pods if the current count is below the desired count, and deletes
        unassigned pods if it is above.
        Also cleans up stale unassigned pods from previous versions.
        """
        # First, clean up stale unassigned pods from previous versions
        stale_pods = self._get_stale_unassigned_bot_runner_pods()
        if stale_pods:
            logger.info("Found %s stale unassigned bot runner pods from previous versions", len(stale_pods))
            for pod in stale_pods:
                if not self._keep_running:
                    logger.info("Shutdown requested, stopping stale pod cleanup")
                    break
                pod_name = pod.metadata.name
                pod_version = (pod.metadata.labels or {}).get("app.kubernetes.io/version", "unknown")
                logger.info("Deleting stale unassigned pod %s (version: %s, current: %s)", pod_name, pod_version, self.app_version)
                self._delete_pod_if_unchanged(pod)

        unassigned_pods = self._get_unassigned_bot_runner_pods()
        if unassigned_pods is None:
            # Don't create pods blindly when we can't see the current pool
            return
        current_count = len(unassigned_pods)

        logger.info(
            "Bot runner pool status: %s unassigned pods (current version), desired: %s",
            current_count,
            desired_count,
        )

        if current_count > desired_count:
            surplus = current_count - desired_count
            logger.info("Deleting %s unassigned bot runner pods to shrink to desired pool size", surplus)
            # Pending pods can't be claimed yet, so give those up first.
            # The delete precondition on resourceVersion keeps us from deleting a pod that was just claimed.
            for pod in sorted(unassigned_pods, key=lambda p: p.status.phase != "Pending")[:surplus]:
                if not self._keep_running:
                    logger.info("Shutdown requested, stopping pool shrink")
                    break
                self._delete_pod_if_unchanged(pod)
            return

        if current_count == desired_count:
            logger.debug("Pool size is sufficient, no new pods needed")
            return

        pods_to_create = desired_count - current_count
        logger.info("Creating %s new bot runner pods to reach desired pool size", pods_to_create)

        for i in range(pods_to_create):
            bot_pod_creator = BotPodCreator()
            if not self._keep_running:
                logger.info("Shutdown requested, stopping pod creation")
                break

            bot_runner_uuid = uuid.uuid4().hex[:12]
            try:
                result = bot_pod_creator.create_bot_pod(
                    bot_pod_cmd=["python", "manage.py", "wait_for_bot_runner_assignment"],
                    bot_pod_name=f"bot-runner-{bot_runner_uuid}",
                    bot_pod_extra_labels={"is-bot-runner": "true"},
                    add_webpage_streamer=False,
                    add_downward_api_volume_for_labels=True,
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
