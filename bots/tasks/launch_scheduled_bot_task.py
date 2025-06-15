import logging

from celery import shared_task

from bots.launch_bot_utils import launch_bot
from bots.models import Bot, BotEventManager, BotEventTypes, BotStates

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def launch_scheduled_bot(self, bot_id: int, bot_join_at: str):
    logger.info(f"Launching scheduled bot {bot_id} with join_at {bot_join_at}")

    # Transition the bot to STAGED
    bot = Bot.objects.get(id=bot_id)

    if bot.state != BotStates.SCHEDULED:
        logger.info(f"Bot {bot_id} ({bot.object_id}) is not in state SCHEDULED, skipping")
        return

    logger.info(f"Transitioning bot {bot_id} ({bot.object_id}) to STAGED")
    BotEventManager.create_event(bot=bot, event_type=BotEventTypes.STAGED, event_metadata={"join_at": bot_join_at})
    launch_bot(bot)
