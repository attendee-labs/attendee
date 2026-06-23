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
        logger.info(f"Bot {bot.object_id} ({bot.id}) launched via Kubernetes: {create_pod_result}")
        if not create_pod_result.get("created"):
            logger.error(f"Bot {bot.object_id} ({bot.id}) failed to launch via Kubernetes.")
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
                logger.error(f"Failed to create fatal error bot not launched event for bot {bot.object_id} ({bot.id}): {str(e)}")
    elif os.getenv("LAUNCH_BOT_METHOD") == "ecs":
        # Launch the bot as a standalone, run-to-completion ECS task (one task per bot).
        from .bot_ecs_task_creator import BotEcsTaskCreator

        ecs_task_creator = BotEcsTaskCreator()
        create_task_result = ecs_task_creator.create_bot_task(
            bot_id=bot.id,
            bot_pod_spec_type=bot.bot_pod_spec_type,
            add_webpage_streamer=bot.should_launch_webpage_streamer(),
            add_persistent_storage=bot.reserve_additional_storage(),
        )
        logger.info(f"Bot {bot.object_id} ({bot.id}) launched via ECS: {create_task_result}")
        if not create_task_result.get("created"):
            logger.error(f"Bot {bot.object_id} ({bot.id}) failed to launch via ECS.")
            try:
                BotEventManager.create_event(
                    bot=bot,
                    event_type=BotEventTypes.FATAL_ERROR,
                    event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED,
                    event_metadata={
                        "create_task_result": json.dumps(create_task_result),
                    },
                )
            except Exception as e:
                logger.error(f"Failed to create fatal error bot not launched event for bot {bot.object_id} ({bot.id}): {str(e)}")
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
