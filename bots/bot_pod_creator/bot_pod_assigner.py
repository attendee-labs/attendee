import logging
import os
import random
import time
from typing import Dict

import requests
from django.conf import settings
from kubernetes import client, config

logger = logging.getLogger(__name__)

# Default port for bot runner HTTP server
BOT_RUNNER_HTTP_PORT = int(os.getenv("BOT_RUNNER_HTTP_PORT", "8080"))


class BotPodAssigner:
    """
    Assigns bots to existing unassigned bot runner pods instead of creating new pods.

    Bot runner pods are pre-created pods that wait for assignment via HTTP.
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

    def _claim_pod_if_unassigned(self, pod_name: str, bot_id: int) -> bool:
        patch = [
            {"op": "test", "path": "/metadata/labels/assigned-bot-id", "value": "none"},
            {"op": "replace", "path": "/metadata/labels/assigned-bot-id", "value": str(bot_id)},
        ]

        try:
            self.v1.patch_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=patch,
            )
            return True
        except client.ApiException as e:
            logger.error("Failed to assign pod %s to bot %s: %s", pod_name, bot_id, e)
            return False

    def _send_assignment_command(self, pod_ip: str, bot_id: int) -> bool:
        """
        Send the assignment command to a bot runner via HTTP POST.

        Returns True if the request was successful.
        """
        url = f"http://{pod_ip}:{BOT_RUNNER_HTTP_PORT}/assign"
        payload = {"bot_id": bot_id}

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()

            logger.info(
                "Sent assignment command to %s (bot_id=%s, status=%s)",
                url,
                bot_id,
                response.status_code,
            )
            return True

        except requests.exceptions.Timeout:
            logger.error("Timeout sending assignment command to %s", url)
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error sending assignment command to %s: %s", url, e)
            return False
        except requests.exceptions.RequestException as e:
            logger.error("Failed to send assignment command to %s: %s", url, e)
            return False

    def assign_bot(self, bot_id: int) -> Dict:
        """
        Assign a bot to an available bot runner pod with one retry.

        Makes an initial attempt, and if it fails, waits 1 second and tries again.

        Args:
            bot_id: The ID of the bot to assign

        Returns:
            A dict with assignment result:
            - assigned: True if assignment was successful
            - pod_name: Name of the assigned pod (if successful)
            - pod_ip: IP address of the assigned pod (if successful)
            - error: Error message (if failed)
        """
        result = self._attempt_to_assign_bot(bot_id)
        if result["assigned"]:
            return result

        logger.info("First assignment attempt failed for bot %s, retrying in 1 second...", bot_id)
        time.sleep(1)

        return self._attempt_to_assign_bot(bot_id)

    def _attempt_to_assign_bot(self, bot_id: int) -> Dict:
        """
        Single attempt to assign a bot to an available bot runner pod.

        Args:
            bot_id: The ID of the bot to assign

        Returns:
            A dict with assignment result:
            - assigned: True if assignment was successful
            - pod_name: Name of the assigned pod (if successful)
            - pod_ip: IP address of the assigned pod (if successful)
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
        pod_ip = pod.status.pod_ip

        if not pod_ip:
            logger.error("Pod %s has no IP address assigned", pod_name)
            return {
                "assigned": False,
                "error": f"Pod {pod_name} has no IP address",
            }

        # Update the pod label to mark it as assigned
        if not self._claim_pod_if_unassigned(pod_name, bot_id):
            return {
                "assigned": False,
                "error": f"Failed to update pod label for {pod_name}",
            }

        # Send the assignment command via HTTP
        if not self._send_assignment_command(pod_ip, bot_id):
            # Rollback the label update
            logger.warning("Rolling back label update for pod %s", pod_name)
            self._release_pod_if_owned(pod_name, bot_id)
            return {
                "assigned": False,
                "error": f"Failed to send assignment command to {pod_name} ({pod_ip})",
            }

        logger.info("Successfully assigned bot %s to pod %s (%s)", bot_id, pod_name, pod_ip)
        return {
            "assigned": True,
            "pod_name": pod_name,
            "pod_ip": pod_ip,
        }

    def _release_pod_if_owned(self, pod_name: str, bot_id: int) -> bool:
        patch = [
            {"op": "test", "path": "/metadata/labels/assigned-bot-id", "value": str(bot_id)},
            {"op": "replace", "path": "/metadata/labels/assigned-bot-id", "value": "none"},
        ]
        try:
            self.v1.patch_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=patch,
            )
            return True
        except client.ApiException as e:
            logger.error("Failed to release pod %s: %s", pod_name, e)
            return False
