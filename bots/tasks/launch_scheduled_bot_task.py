import logging

from celery import shared_task
from django.db import transaction

from bots.launch_bot_utils import launch_bot
from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes, BotStates

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def launch_scheduled_bot(self, bot_id: int, bot_join_at: str):
    logger.info(f"Launching scheduled bot {bot_id} with join_at {bot_join_at}")

    # Serialize the state check and transition across duplicate task deliveries.
    with transaction.atomic():
        bot = Bot.objects.select_for_update(no_key=True).get(id=bot_id)

        if bot.state != BotStates.SCHEDULED:
            logger.info(f"Bot {bot_id} ({bot.object_id}) is not in state SCHEDULED, skipping")
            return

        if bot.project.organization.out_of_credits():
            logger.error(f"Bot {bot_id} ({bot.object_id}) was not launched because the organization ({bot.project.organization.id}) has insufficient credits.")
            BotEventManager.create_event(bot=bot, event_type=BotEventTypes.FATAL_ERROR, event_sub_type=BotEventSubTypes.FATAL_ERROR_OUT_OF_CREDITS)
            return

        logger.info(f"Transitioning bot {bot_id} ({bot.object_id}) to STAGED")
        BotEventManager.create_event(bot=bot, event_type=BotEventTypes.STAGED, event_metadata={"join_at": bot_join_at})

    # Release the row lock before launching, which may call the Kubernetes API.
    launch_bot(bot)
