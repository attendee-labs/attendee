import logging
import time

from celery import shared_task
from kubernetes import client, config

from bots.k8s_utils import find_bot_pod_name
from bots.models import Bot, BotEventTypes

logger = logging.getLogger(__name__)
from django.conf import settings

from bots.bot_pod_creator import BotPodCreator


@shared_task(bind=True, soft_time_limit=3600)
def restart_bot_pod(self, bot_id):
    """
    Restart a bot pod.
    """

    logger.info(f"Restarting bot pod for bot {bot_id}")

    bot = Bot.objects.get(id=bot_id)

    last_bot_event = bot.last_bot_event()

    if last_bot_event.event_type not in (BotEventTypes.JOIN_REQUESTED, BotEventTypes.STAGED):
        logger.info(f"Bot {bot_id} is not in JOINING or STAGED state, so not restarting pod")
        return

    # Initialize kubernetes client
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    v1 = client.CoreV1Api()
    namespace = settings.BOT_POD_NAMESPACE

    # Check if a pod already exists for this bot
    pod_name = bot.k8s_pod_name()
    try:
        pod_name = find_bot_pod_name(v1, namespace, bot)

        # Directly read the specific pod by name instead of listing all pods
        v1.read_namespaced_pod(name=pod_name, namespace=namespace)

        # Delete the pod if it exists (we'll only get here if the pod exists)
        logger.info(f"Found existing pod {pod_name} for bot {bot_id}, deleting it before creating a new one")
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=60)

        # Sleep until the pod is no longer found
        num_retries = 20
        for i in range(num_retries):
            try:
                v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            except client.ApiException as e:
                if e.status == 404:
                    logger.info(f"Pod {pod_name} deleted successfully")
                    break
                else:
                    logger.error(f"Error checking for existing pod: {str(e)}")
            if i == num_retries - 1:
                logger.error(f"Pod {pod_name} did not delete after {num_retries} retries")
                raise Exception(f"Pod {pod_name} did not delete after {num_retries} retries")
            time.sleep(5)

    except client.ApiException as e:
        if e.status == 404:
            # Pod doesn't exist - this is fine, just continue
            logger.info(f"Pod {pod_name} not found, no need to delete")
        else:
            # Some other API error occurred
            logger.error(f"Error checking for existing pod: {str(e)}")

    last_bot_event.requested_bot_action_taken_at = None
    if "pod_recreations" not in last_bot_event.metadata:
        last_bot_event.metadata["pod_recreations"] = []
    last_bot_event.metadata["pod_recreations"].append(int(time.time()))
    last_bot_event.save()

    bot.first_heartbeat_timestamp = None
    bot.last_heartbeat_timestamp = None
    bot.save()

    bot_pod_creator = BotPodCreator()
    bot_pod_create_result = bot_pod_creator.create_bot_pod(
        bot_pod_cmd=bot.k8s_pod_cmd(),
        bot_pod_name=bot.k8s_pod_name(),
        bot_pod_cpu_request=bot.cpu_request(),
        add_webpage_streamer=bot.should_launch_webpage_streamer(),
        add_persistent_storage=bot.reserve_additional_storage(),
        bot_pod_spec_type=bot.bot_pod_spec_type,
        assigned_bot_object_id=bot.object_id,
    )

    logger.info(f"Bot pod create result: {bot_pod_create_result}")
