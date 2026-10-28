import json
import logging
import os

from django.conf import settings

from bots.models import BotEventManager, BotEventSubTypes, BotEventTypes

logger = logging.getLogger(__name__)


def launch_bot(bot):
    # If this instance is running in Kubernetes, use the Kubernetes pod creator
    # which spins up a new pod for the bot
    logger.info(f"Launching bot {bot.object_id} ({bot.id}) with method {os.getenv('LAUNCH_BOT_METHOD', 'celery')}")
    if os.getenv("LAUNCH_BOT_METHOD") == "kubernetes":
        if bot.should_launch_bot_pod_with_runner_pool():
            _launch_bot_pod_with_runner_pool(bot)
        else:
            _launch_bot_pod_directly(bot)
    elif os.getenv("LAUNCH_BOT_METHOD") == "docker-compose-multi-host":
        # Launch bot via dedicated Celery app (bot_launcher) which uses ephemeral Docker containers
        from .tasks.run_bot_in_ephemeral_container_task import run_bot_in_ephemeral_container

        # Assign task to specific queue so that it gets picked up by a worker running in a bot launcher VM.
        run_bot_in_ephemeral_container.apply_async(args=[bot.id], queue="bot_launcher_vm")
        logger.info(f"Bot {bot.object_id} ({bot.id}) launched via run_bot_in_ephemeral_container task in queue bot_launcher_vm")
    else:
        # Default to launching bot via celery
        from .tasks.run_bot_task import run_bot

        run_bot.delay(bot.id)


def _launch_bot_pod_with_runner_pool(bot):
    """
    Launch a bot by assigning it to an existing bot runner pod from the pool.
    Falls back to creating a new pod if no runners are available.
    """
    from .bot_pod_creator import BotRunnerPodAssigner

    assigner = BotRunnerPodAssigner()
    assign_result = assigner.assign_bot(bot_object_id=bot.object_id)

    if assign_result.get("assigned"):
        logger.info(
            "Bot %s (%s) assigned to bot runner pod %s",
            bot.object_id,
            bot.id,
            assign_result.get("pod_name"),
        )
    elif assign_result.get("outcome_unknown"):
        logger.error(
            "Bot %s (%s) may or may not have been assigned to a runner pod: %s. Not falling back to pod creation.",
            bot.object_id,
            bot.id,
            assign_result.get("error"),
        )
    else:
        logger.warning(
            "Bot %s (%s) could not be assigned to a runner pod: %s. Falling back to pod creation.",
            bot.object_id,
            bot.id,
            assign_result.get("error"),
        )
        # Fall back to creating a new pod
        _launch_bot_pod_directly(bot)


def _launch_bot_pod_directly(bot):
    from .bot_pod_creator import BotPodCreator

    bot_pod_creator = BotPodCreator()
    create_pod_result = bot_pod_creator.create_bot_pod(
        bot_pod_cmd=bot.k8s_pod_cmd(),
        bot_pod_name=bot.k8s_pod_name(),
        bot_pod_cpu_request=bot.cpu_request(),
        add_webpage_streamer=bot.should_launch_webpage_streamer(),
        add_persistent_storage=bot.reserve_additional_storage(),
        bot_pod_spec_type=bot.bot_pod_spec_type,
        assigned_bot_object_id=bot.object_id,
    )
    logger.info(f"Bot {bot.object_id} ({bot.id}) launched via Kubernetes: {create_pod_result}")
    if not create_pod_result.get("created"):
        logger.error(f"Bot {bot.object_id} ({bot.id}) failed to launch via Kubernetes.")
        try:
            event_metadata = None
            if settings.STORE_INFRASTRUCTURE_INFORMATION_IN_BOT_EVENT_METADATA:
                event_metadata = {"create_pod_result": json.dumps(create_pod_result)}

            BotEventManager.create_event(
                bot=bot,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED,
                event_metadata=event_metadata,
            )
        except Exception as e:
            logger.error(f"Failed to create fatal error bot not launched event for bot {bot.object_id} ({bot.id}): {str(e)}")


def launch_adhoc_bot_from_view(bot):
    """Launch an adhoc (non-scheduled) bot from a webserver view.

    When ``settings.LAUNCH_ADHOC_BOTS_ASYNC`` is enabled, the actual launch is
    handed off to a Celery worker so the request thread returns immediately.
    Otherwise the bot is launched inline in the current (webserver) process.
    """
    if settings.LAUNCH_ADHOC_BOTS_ASYNC:
        from .tasks.launch_adhoc_bot_task import launch_adhoc_bot

        launch_adhoc_bot.delay(bot.id)
    else:
        launch_bot(bot)
