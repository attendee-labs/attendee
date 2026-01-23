import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="bot_launcher.launch_bot_from_queue")
def launch_bot_from_queue(self, bot_id: int):
    """
    Tâche Celery dédiée qui lance run_bot pour un bot_id donné.
    Cette tâche tourne dans un worker Celery dédié avec -c 1.
    """
    logger.info(f"Launching bot {bot_id} from dedicated Celery worker (bot_launcher)")

    try:
        from bots.tasks.run_bot_task import run_bot

        # Appel direct de la fonction run_bot
        result = run_bot.run(bot_id)
        logger.info(f"Bot task completed for bot_id: {bot_id}, result: {result}")
        return result
    except Exception as e:
        logger.error(f"Error processing bot task for bot_id: {bot_id}: {str(e)}", exc_info=True)
        raise

