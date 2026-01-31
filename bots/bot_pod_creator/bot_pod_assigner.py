import json
import logging
import os
import random
from typing import Dict, Optional

import redis
from django.conf import settings
from kubernetes import client, config

logger = logging.getLogger(__name__)


class BotPodAssigner:
    """
    Assigns bots to existing unassigned bot runner pods instead of creating new pods.

    Bot runner pods are pre-created pods that wait for assignment via Redis.
    This class finds an available runner and sends the assignment command.
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

        # Initialize Redis client
        redis_url = os.getenv("REDIS_URL") + ("?ssl_cert_reqs=none" if os.getenv("DISABLE_REDIS_SSL") else "")
        self.redis_client = redis.from_url(redis_url)

        logger.info("BotPodAssigner initialized for namespace %s with version %s", self.namespace, self.app_version)

    def _get_unassigned_bot_runner_pods(self) -> list:
        """
        Get all unassigned bot runner pods in the namespace.

        A pod is considered an unassigned bot runner if:
        - It has the label "is-bot-runner=true"
        - It has the annotation "assigned-bot-id" set to empty string
        - It is in Running phase (ready to accept assignments)
        - It has the same app.kubernetes.io/version label as this pod assigner
        """
        try:
            pods = self.v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector="is-bot-runner=true",
            )

            unassigned_pods = []
            for pod in pods.items:
                # Only consider pods that are Running
                if pod.status.phase != "Running":
                    continue

                # Only consider pods that have the same release version as this pod assigner
                labels = pod.metadata.labels or {}
                pod_version = labels.get("app.kubernetes.io/version")
                if pod_version != self.app_version:
                    continue

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

    def _extract_runner_uuid_from_pod_name(self, pod_name: str) -> Optional[str]:
        """
        Extract the bot runner UUID from a pod name.

        Pod names follow the pattern: bot-runner-{uuid}
        """
        prefix = "bot-runner-"
        if pod_name.startswith(prefix):
            return pod_name[len(prefix) :]
        return None

    def _claim_pod_if_unassigned(self, pod_name: str, bot_id: int) -> bool:
        patch = [
            {"op": "test", "path": "/metadata/annotations/assigned-bot-id", "value": ""},
            {"op": "replace", "path": "/metadata/annotations/assigned-bot-id", "value": str(bot_id)},
        ]

        try:
            self.v1.patch_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=patch,
                _content_type="application/json-patch+json",
            )
            return True
        except client.ApiException as e:
            # If someone else claimed it first, the "test" fails and the API rejects the patch.
            # Status can vary by k8s/proxy (commonly 409/422). Treat as "not claimed".
            if e.status in (409, 422):
                return False
            raise

    def _send_assignment_command(self, bot_runner_uuid: str, bot_id: int) -> bool:
        """
        Send the assignment command to a bot runner via Redis.

        Returns True if the message was published successfully.
        """
        channel_name = f"bot_runner_{bot_runner_uuid}"
        message = json.dumps({"command": "assign", "bot_id": bot_id})

        try:
            num_subscribers = self.redis_client.publish(channel_name, message)
            logger.info(
                "Sent assignment command to channel %s (bot_id=%s, subscribers=%s)",
                channel_name,
                bot_id,
                num_subscribers,
            )
            return num_subscribers > 0

        except redis.RedisError as e:
            logger.error("Failed to send assignment command: %s", e)
            return False

    def assign_bot(self, bot_id: int) -> Dict:
        """
        Assign a bot to an available bot runner pod.

        Args:
            bot_id: The ID of the bot to assign

        Returns:
            A dict with assignment result:
            - assigned: True if assignment was successful
            - pod_name: Name of the assigned pod (if successful)
            - bot_runner_uuid: UUID of the assigned runner (if successful)
            - error: Error message (if failed)
        """
        # Find an unassigned bot runner pod
        unassigned_pods = self._get_unassigned_bot_runner_pods()

        if not unassigned_pods:
            logger.warning("No unassigned bot runner pods available for bot %s", bot_id)
            return {
                "assigned": False,
                "error": "No unassigned bot runner pods available",
            }

        # Use a random unassigned pod
        pod = random.choice(unassigned_pods)
        pod_name = pod.metadata.name
        bot_runner_uuid = self._extract_runner_uuid_from_pod_name(pod_name)

        if not bot_runner_uuid:
            logger.error("Could not extract runner UUID from pod name: %s", pod_name)
            return {
                "assigned": False,
                "error": f"Invalid pod name format: {pod_name}",
            }

        # Update the pod annotation to mark it as assigned
        if not self._claim_pod_if_unassigned(pod_name, bot_id):
            return {
                "assigned": False,
                "error": f"Failed to update pod annotation for {pod_name}",
            }

        # Send the assignment command via Redis
        if not self._send_assignment_command(bot_runner_uuid, bot_id):
            # Rollback the annotation update
            logger.warning("Rolling back annotation update for pod %s", pod_name)
            self._release_pod_if_owned(pod_name, bot_id)
            return {
                "assigned": False,
                "error": f"Failed to send assignment command to {bot_runner_uuid}",
            }

        logger.info("Successfully assigned bot %s to pod %s", bot_id, pod_name)
        return {
            "assigned": True,
            "pod_name": pod_name,
            "bot_runner_uuid": bot_runner_uuid,
        }

    def _release_pod_if_owned(self, pod_name: str, bot_id: int) -> bool:
        patch = [
            {"op": "test", "path": "/metadata/annotations/assigned-bot-id", "value": str(bot_id)},
            {"op": "replace", "path": "/metadata/annotations/assigned-bot-id", "value": ""},
        ]
        try:
            self.v1.patch_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=patch,
                _content_type="application/json-patch+json",
            )
            return True
        except client.ApiException as e:
            if e.status in (409, 422):
                return False
            raise
