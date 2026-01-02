import json
import logging
import os

from bots.models import BotEventManager, BotEventSubTypes, BotEventTypes

logger = logging.getLogger(__name__)


def launch_bot(bot):
    # If this instance is running in Kubernetes, use the Kubernetes pod creator
    # which spins up a new pod for the bot
    if os.getenv("LAUNCH_BOT_METHOD") == "kubernetes":
        # Check if we should use the bot runner pool instead of creating new pods
        if os.getenv("USE_BOT_RUNNER_POOL", "false").lower() == "true":
            _launch_bot_via_runner_pool(bot)
        else:
            _launch_bot_via_pod_creator(bot)
    else:
        # Default to launching bot via celery
        from .tasks.run_bot_task import run_bot

        run_bot.delay(bot.id)


def _launch_bot_via_runner_pool(bot):
    """
    Launch a bot by assigning it to an existing bot runner pod from the pool.
    Falls back to creating a new pod if no runners are available.
    """
    from .bot_pod_creator import BotPodAssigner

    assigner = BotPodAssigner()
    assign_result = assigner.assign_bot(bot_id=bot.id)

    if assign_result.get("assigned"):
        logger.info(
            "Bot %s (%s) assigned to bot runner pod %s",
            bot.object_id,
            bot.id,
            assign_result.get("pod_name"),
        )
    else:
        logger.warning(
            "Bot %s (%s) could not be assigned to a runner pod: %s. Falling back to pod creation.",
            bot.object_id,
            bot.id,
            assign_result.get("error"),
        )
        # Fall back to creating a new pod
        _launch_bot_via_pod_creator(bot)


def _launch_bot_via_pod_creator(bot):
    """
    Launch a bot by creating a new Kubernetes pod.
    """
    from .bot_pod_creator import BotPodCreator

    bot_pod_creator = BotPodCreator()
    create_pod_result = bot_pod_creator.create_bot_pod(
        bot_id=bot.id,
        bot_name=bot.k8s_pod_name(),
        bot_cpu_request=bot.cpu_request(),
        add_webpage_streamer=bot.should_launch_webpage_streamer(),
        add_persistent_storage=bot.reserve_additional_storage(),
        bot_pod_spec_type=bot.bot_pod_spec_type,
    )
    logger.info("Bot %s (%s) launched via Kubernetes: %s", bot.object_id, bot.id, create_pod_result)

    if not create_pod_result.get("created"):
        logger.error("Bot %s (%s) failed to launch via Kubernetes.", bot.object_id, bot.id)
        try:
            BotEventManager.create_event(
                bot=bot,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED,
                event_metadata={
                    "create_pod_result": json.dumps(create_pod_result),
                },
            )
        except Exception as e:
            logger.error(
                "Failed to create fatal error bot not launched event for bot %s (%s): %s",
                bot.object_id,
                bot.id,
                str(e),
            )
