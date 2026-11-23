import logging
import os
import random
import time
from typing import Dict

import redis
from django.conf import settings
from kubernetes import client, config

logger = logging.getLogger(__name__)

# Redis hash of how many bot launches asked for a runner, by outcome ("assigned", "not_assigned" or
# "outcome_unknown"). The pool manager sizes the pool from how fast these grow. They are running totals
# that are never reset, so only the difference between two readings means anything.
BOT_RUNNER_POOL_LAUNCH_COUNTS_REDIS_KEY = "bot_runner_pool:launch_counts"

_redis_client = None


def _get_redis_client():
    # Shared across the process. The short timeouts keep a slow Redis from holding up bot launches.
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS, socket_timeout=2, socket_connect_timeout=2)
    return _redis_client


def _record_bot_runner_pool_launch(outcome: str):
    # This is only used for sizing the pool, so a Redis problem must never fail the launch
    try:
        _get_redis_client().hincrby(BOT_RUNNER_POOL_LAUNCH_COUNTS_REDIS_KEY, outcome, 1)
    except Exception as e:
        logger.warning("Failed to record bot runner pool launch with outcome %s: %s", outcome, e)


def get_bot_runner_pool_launch_counts() -> Dict[str, int]:
    """Returns the running total of bot launches that asked for a runner, by outcome. Raises redis.RedisError if Redis can't be reached."""
    launch_counts = _get_redis_client().hgetall(BOT_RUNNER_POOL_LAUNCH_COUNTS_REDIS_KEY)
    return {outcome.decode(): int(count) for outcome, count in launch_counts.items()}


class ClaimOutcomeUnknownError(Exception):
    pass


class BotRunnerPodAssigner:
    """
    Assigns bots to existing unassigned bot runner pods instead of creating new pods.

    Bot runner pods are pre-created pods that watch their own assigned-bot-id label
    through a downward API volume. This class finds an available runner and sets that label.
    """

    def __init__(self):
        # Initialize Kubernetes client
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

        logger.info("BotRunnerPodAssigner initialized for namespace %s with version %s", self.namespace, self.app_version)

    def _get_unassigned_bot_runner_pods(self) -> list:
        """
        Get all unassigned bot runner pods in the namespace.

        A pod is considered an unassigned bot runner if:
        - It has the label "is-bot-runner=true"
        - It has the label "assigned-bot-id" set to "none"
        - It is in Running phase (ready to accept assignments)
        - It is not being deleted (no deletion_timestamp)
        - It has the same app.kubernetes.io/version label as this pod assigner
        """
        try:
            pods = self.v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"is-bot-runner=true,assigned-bot-id=none,app.kubernetes.io/version={self.app_version}",
                field_selector="status.phase=Running",
            )

            unassigned_pods = []
            for pod in pods.items:
                # Terminating pods (eviction, node scale-down, etc.) still report Running until they exit.
                # deletionTimestamp isn't a selectable field, so this filter has to stay client-side.
                if pod.metadata.deletion_timestamp is not None:
                    continue

                unassigned_pods.append(pod)

            return unassigned_pods

        except client.ApiException as e:
            logger.error("Failed to list bot runner pods: %s", e)
            return []

    def _claim_pod_if_unassigned(self, pod_name: str, resource_version: str, bot_object_id: str) -> bool:
        # Testing resourceVersion rejects the claim if the pod changed since it was listed,
        # e.g. a deletionTimestamp was set after we filtered out terminating pods.
        patch = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
            {"op": "test", "path": "/metadata/labels/assigned-bot-id", "value": "none"},
            {"op": "replace", "path": "/metadata/labels/assigned-bot-id", "value": bot_object_id},
        ]

        # An error response doesn't mean the patch wasn't applied: the API server can commit it and still
        # return e.g. a 504, or commit it after responding. Because of the resourceVersion test, the patch can
        # only ever apply while the pod is unchanged, so we read the pod back to find out where we stand.
        for attempt in range(2):
            try:
                self.v1.patch_namespaced_pod(
                    name=pod_name,
                    namespace=self.namespace,
                    body=patch,
                )
                return True
            except client.ApiException as e:
                logger.error("Failed to assign pod %s to bot %s (attempt %d): %s", pod_name, bot_object_id, attempt + 1, e)

            try:
                pod = self.v1.read_namespaced_pod(name=pod_name, namespace=self.namespace)
            except client.ApiException as e:
                if e.status == 404:
                    return False
                raise ClaimOutcomeUnknownError(f"Could not read pod {pod_name} to verify claim for bot {bot_object_id}: {e}")

            assigned_bot_id = (pod.metadata.labels or {}).get("assigned-bot-id")
            if assigned_bot_id == bot_object_id:
                logger.info("Claim of pod %s for bot %s was applied despite the error response", pod_name, bot_object_id)
                return True
            if pod.metadata.resource_version != resource_version:
                return False
            # The pod is unchanged (so still assigned-bot-id=none), and our patch may still be applied.
            # Resending it to the same pod is safe because at most one of the two can apply.

        raise ClaimOutcomeUnknownError(f"Claim of pod {pod_name} for bot {bot_object_id} may still be applied")

    def assign_bot(self, bot_object_id: str) -> Dict:
        """
        Assign a bot to an available bot runner pod with one retry.

        Makes an initial attempt, and if it fails, waits 1 second and tries again.

        Args:
            bot_object_id: The object ID of the bot to assign

        Returns:
            A dict with assignment result:
            - assigned: True if assignment was successful
            - pod_name: Name of the assigned pod (if successful)
            - error: Error message (if failed)
            - outcome_unknown: True if a claim may still be applied to a runner pod. The bot must not be
              launched anywhere else, or it could end up running twice.
        """
        result = self._attempt_to_assign_bot(bot_object_id)
        if not result["assigned"] and not result.get("outcome_unknown"):
            logger.info("First assignment attempt failed for bot %s, retrying in 1 second...", bot_object_id)
            time.sleep(1)
            result = self._attempt_to_assign_bot(bot_object_id)

        # Every launch is recorded, not just the ones that got a runner: a launch that found the pool
        # empty is the clearest sign that the pool is too small.
        if result["assigned"]:
            _record_bot_runner_pool_launch("assigned")
        elif result.get("outcome_unknown"):
            _record_bot_runner_pool_launch("outcome_unknown")
        else:
            _record_bot_runner_pool_launch("not_assigned")

        return result

    def _attempt_to_assign_bot(self, bot_object_id: str) -> Dict:
        """
        Single attempt to assign a bot to an available bot runner pod.

        Args:
            bot_object_id: The object ID of the bot to assign

        Returns:
            A dict with assignment result:
            - assigned: True if assignment was successful
            - pod_name: Name of the assigned pod (if successful)
            - error: Error message (if failed)
        """
        # Find an unassigned bot runner pod
        unassigned_pods = self._get_unassigned_bot_runner_pods()

        if not unassigned_pods:
            logger.warning("No unassigned bot runner pods available for bot %s", bot_object_id)
            return {
                "assigned": False,
                "error": "No unassigned bot runner pods available",
            }

        # Use a random unassigned pod
        pod = random.choice(unassigned_pods)
        pod_name = pod.metadata.name

        # The bot runner picks up the label change through its downward API volume
        try:
            claimed = self._claim_pod_if_unassigned(pod_name, pod.metadata.resource_version, bot_object_id)
        except ClaimOutcomeUnknownError as e:
            logger.error("%s", e)
            return {
                "assigned": False,
                "outcome_unknown": True,
                "error": str(e),
            }
        if not claimed:
            return {
                "assigned": False,
                "error": f"Failed to update pod label for {pod_name}",
            }

        logger.info("Successfully assigned bot %s to pod %s", bot_object_id, pod_name)
        return {
            "assigned": True,
            "pod_name": pod_name,
        }
