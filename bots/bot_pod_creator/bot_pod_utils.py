import logging
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from kubernetes import client

logger = logging.getLogger(__name__)


@dataclass
class PodInfo:
    """Information about a pod that a bot is running on."""

    pod_name: str
    namespace: str


def locate_pod_for_bot(v1: client.CoreV1Api, bot) -> Optional[PodInfo]:
    """
    Locate the pod that a bot is running on.

    Checks both bot-specific pods and bot runner pods.

    A bot can run on either:
    1. A bot-specific pod: Named "bot-pod-{bot.id}-{bot.object_id}"
    2. A bot runner pod: Named "bot-runner-{uuid}" with annotation "assigned-bot-id" set to the bot's id

    Args:
        v1: Kubernetes CoreV1Api client
        bot: The Bot model instance

    Returns:
        PodInfo if a pod is found, None otherwise
    """
    namespace = settings.BOT_POD_NAMESPACE

    # First, check for a bot-specific pod
    bot_specific_pod_name = bot.k8s_pod_name()
    if _pod_exists(v1, bot_specific_pod_name, namespace):
        logger.info("Found bot-specific pod %s for bot %s", bot_specific_pod_name, bot.id)
        return PodInfo(pod_name=bot_specific_pod_name, namespace=namespace)

    # If not found, check for a bot runner pod assigned to this bot
    bot_runner_pod = _find_bot_runner_pod_for_bot(v1, bot.id, namespace)
    if bot_runner_pod:
        logger.info("Found bot runner pod %s for bot %s", bot_runner_pod, bot.id)
        return PodInfo(pod_name=bot_runner_pod, namespace=namespace)

    logger.info("No pod found for bot %s", bot.id)
    return None


def _pod_exists(v1: client.CoreV1Api, pod_name: str, namespace: str) -> bool:
    """Check if a pod with the given name exists in the namespace."""
    try:
        v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        return True
    except client.ApiException as e:
        if e.status == 404:
            return False
        logger.error("Error checking if pod %s exists: %s", pod_name, e)
        raise


def _find_bot_runner_pod_for_bot(v1: client.CoreV1Api, bot_id: int, namespace: str) -> Optional[str]:
    """
    Find a bot runner pod that is assigned to the given bot.

    Args:
        v1: Kubernetes CoreV1Api client
        bot_id: The ID of the bot
        namespace: The Kubernetes namespace to search in

    Returns:
        The pod name if found, None otherwise
    """
    try:
        pods = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector="is-bot-runner=true",
        )

        for pod in pods.items:
            annotations = pod.metadata.annotations or {}
            assigned_bot_id = annotations.get("assigned-bot-id")
            if assigned_bot_id == str(bot_id):
                return pod.metadata.name

        return None

    except client.ApiException as e:
        logger.error("Failed to list bot runner pods: %s", e)
        return None
