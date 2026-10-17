import logging
import os
import random
import time
from typing import Dict

from django.conf import settings
from kubernetes import client, config

logger = logging.getLogger(__name__)


class BotPodAssigner:
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

        logger.info("BotPodAssigner initialized for namespace %s with version %s", self.namespace, self.app_version)

    def _get_unassigned_bot_runner_pods(self) -> list:
        """
        Get all unassigned bot runner pods in the namespace.

        A pod is considered an unassigned bot runner if:
        - It has the label "is-bot-runner=true"
        - It has the label "assigned-bot-id" set to "none"
        - It is in Running phase (ready to accept assignments)
        - It has the same app.kubernetes.io/version label as this pod assigner
        """
        try:
            pods = self.v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"is-bot-runner=true,assigned-bot-id=none,app.kubernetes.io/version={self.app_version}",
            )

            unassigned_pods = []
            for pod in pods.items:
                # Only consider pods that are Running
                if pod.status.phase != "Running":
                    continue

                unassigned_pods.append(pod)

            return unassigned_pods

        except client.ApiException as e:
            logger.error("Failed to list bot runner pods: %s", e)
            return []

    def _claim_pod_if_unassigned(self, pod_name: str, bot_object_id: str) -> bool:
        patch = [
            {"op": "test", "path": "/metadata/labels/assigned-bot-id", "value": "none"},
            {"op": "replace", "path": "/metadata/labels/assigned-bot-id", "value": bot_object_id},
        ]

        try:
            self.v1.patch_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=patch,
            )
            return True
        except client.ApiException as e:
            logger.error("Failed to assign pod %s to bot %s: %s", pod_name, bot_object_id, e)
            return False

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
        """
        result = self._attempt_to_assign_bot(bot_object_id)
        if result["assigned"]:
            return result

        logger.info("First assignment attempt failed for bot %s, retrying in 1 second...", bot_object_id)
        time.sleep(1)

        return self._attempt_to_assign_bot(bot_object_id)

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
        if not self._claim_pod_if_unassigned(pod_name, bot_object_id):
            return {
                "assigned": False,
                "error": f"Failed to update pod label for {pod_name}",
            }

        logger.info("Successfully assigned bot %s to pod %s", bot_object_id, pod_name)
        return {
            "assigned": True,
            "pod_name": pod_name,
        }
