import logging
import os
import time

from celery import shared_task
from django.conf import settings
from kubernetes import client, config

from bots.models import Bot, BotEventTypes

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def restart_bot_pod(self, bot_id):
    """
    Restart a bot's pod (Kubernetes) or task (ECS).
    """

    logger.info(f"Restarting bot pod for bot {bot_id}")

    bot = Bot.objects.get(id=bot_id)

    last_bot_event = bot.last_bot_event()

    if last_bot_event.event_type != BotEventTypes.JOIN_REQUESTED:
        logger.info(f"Bot {bot_id} is not in JOINING state, so not restarting pod")
        return

    # Tear down any existing infra for this bot so it can be re-launched cleanly.
    if os.getenv("LAUNCH_BOT_METHOD") == "ecs":
        _stop_existing_bot_task_and_wait(bot)
    else:
        _delete_existing_bot_pod_and_wait(bot)

    last_bot_event.requested_bot_action_taken_at = None
    if "pod_recreations" not in last_bot_event.metadata:
        last_bot_event.metadata["pod_recreations"] = []
    last_bot_event.metadata["pod_recreations"].append(int(time.time()))
    last_bot_event.save()

    bot.first_heartbeat_timestamp = None
    bot.last_heartbeat_timestamp = None
    bot.save()

    create_result = _relaunch_bot(bot)
    logger.info(f"Bot pod create result: {create_result}")


def _relaunch_bot(bot):
    if os.getenv("LAUNCH_BOT_METHOD") == "ecs":
        from bots.bot_ecs_task_creator import BotEcsTaskCreator

        return BotEcsTaskCreator().create_bot_task(
            bot_id=bot.id,
            bot_pod_spec_type=bot.bot_pod_spec_type,
            add_webpage_streamer=bot.should_launch_webpage_streamer(),
            add_persistent_storage=bot.reserve_additional_storage(),
        )

    from bots.bot_pod_creator import BotPodCreator

    return BotPodCreator().create_bot_pod(
        bot_id=bot.id,
        bot_name=bot.k8s_pod_name(),
        bot_cpu_request=bot.cpu_request(),
        add_webpage_streamer=bot.should_launch_webpage_streamer(),
        add_persistent_storage=bot.reserve_additional_storage(),
        bot_pod_spec_type=bot.bot_pod_spec_type,
    )


def _delete_existing_bot_pod_and_wait(bot):
    """Delete a bot's Kubernetes pod (if present) and wait for it to disappear so
    the deterministic pod name is free to be re-used by the relaunch."""
    # Initialize kubernetes client
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    v1 = client.CoreV1Api()
    namespace = settings.BOT_POD_NAMESPACE

    # Check if pod already exists with this name
    pod_name = bot.k8s_pod_name()
    try:
        # Directly read the specific pod by name instead of listing all pods
        v1.read_namespaced_pod(name=pod_name, namespace=namespace)

        # Delete the pod if it exists (we'll only get here if the pod exists)
        logger.info(f"Found existing pod {pod_name}, deleting it before creating a new one")
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


def _stop_existing_bot_task_and_wait(bot):
    """Stop a bot's ECS task(s) (if any) and wait until none are running before relaunch."""
    from bots.bot_ecs_task_creator import BotEcsTaskCreator

    creator = BotEcsTaskCreator()
    arns = creator.find_bot_task_arns(bot.id)
    if not arns:
        logger.info(f"No running ECS task for bot {bot.id}, no need to stop")
        return

    logger.info(f"Found existing ECS task(s) {arns} for bot {bot.id}, stopping before relaunch")
    creator.stop_bot_task(bot.id, reason="Restarting bot via restart_bot_pod")

    # Wait until no running task remains so the relaunch starts clean.
    num_retries = 20
    for i in range(num_retries):
        if not creator.find_bot_task_arns(bot.id):
            logger.info(f"ECS task(s) for bot {bot.id} stopped successfully")
            break
        if i == num_retries - 1:
            logger.error(f"ECS task(s) for bot {bot.id} did not stop after {num_retries} retries")
            raise Exception(f"ECS task(s) for bot {bot.id} did not stop after {num_retries} retries")
        time.sleep(5)
