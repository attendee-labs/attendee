import logging
import os

logger = logging.getLogger(__name__)


def launch_bot(bot):
    # If this instance is running in Kubernetes, use the Kubernetes pod creator
    # which spins up a new pod for the bot
    if os.getenv("LAUNCH_BOT_METHOD") == "kubernetes":
        from .bot_pod_creator import BotPodCreator

        bot_pod_creator = BotPodCreator()
        create_pod_result = bot_pod_creator.create_bot_pod(bot_id=bot.id, bot_name=bot.k8s_pod_name(), bot_cpu_request=bot.cpu_request())
        logger.info(f"Bot {bot.id} launched via Kubernetes: {create_pod_result}")
    else:
        # Default to launching bot via celery
        from .tasks.run_bot_task import run_bot

        run_bot.delay(bot.id)
