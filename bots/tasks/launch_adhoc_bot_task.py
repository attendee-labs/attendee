import logging

from celery import shared_task

from bots.launch_bot_utils import launch_bot
from bots.models import Bot, BotStates

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def launch_adhoc_bot(self, bot_id: int):
    logger.info(f"Launching adhoc bot {bot_id}")

    bot = Bot.objects.get(id=bot_id)

    if bot.state != BotStates.JOINING:
        logger.info(f"Bot {bot_id} ({bot.object_id}) is not in state JOINING, skipping")
        return

    launch_bot(bot)
