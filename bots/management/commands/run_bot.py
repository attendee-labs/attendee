import logging

from django.core.management.base import BaseCommand

from bots.tasks import run_bot  # Import your task

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the celery task synchronously on a given bot that is already created"

    def add_arguments(self, parser):
        # Add any arguments you need
        parser.add_argument("--botid", type=int, help="Bot ID")

    def handle(self, *args, **options):
        logger.info("Running run bot task...")

        # Call your task directly
        result = run_bot.run(options["botid"])

        logger.info(f"Run bot task completed with result: {result}")
