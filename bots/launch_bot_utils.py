import json
import logging
import os

from bots.models import BotEventManager, BotEventSubTypes, BotEventTypes

logger = logging.getLogger(__name__)


def launch_bot(bot):
    # If this instance is running in Kubernetes, use the Kubernetes pod creator
    # which spins up a new pod for the bot
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
    else:
        # Default to launching bot via celery
        from .tasks.run_bot_task import run_bot

        run_bot.delay(bot.id)
