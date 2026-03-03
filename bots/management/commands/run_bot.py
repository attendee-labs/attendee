import logging

from django.core.management.base import BaseCommand

from bots.bot_controller import BotController

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs a bot directly (used by the Celery task to isolate the bot in its own process)"

    def add_arguments(self, parser):
        parser.add_argument("--botid", type=int, required=True, help="Bot ID")

    def handle(self, *args, **options):
        bot_id = options["botid"]
        logger.info("Running bot %s", bot_id)
        bot_controller = BotController(bot_id)
        bot_controller.run()
