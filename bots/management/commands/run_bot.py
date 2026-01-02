import json
import logging
import os
import signal

import redis
from django.core.management.base import BaseCommand

from bots.tasks import run_bot  # Import your task

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the celery task synchronously on a given bot that is already created"

    # Graceful shutdown flag
    _keep_running = True

    def _graceful_exit(self, signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        self._keep_running = False

    def add_arguments(self, parser):
        parser.add_argument("--botid", type=int, help="Bot ID")
        parser.add_argument("--botrunneruuid", type=str, help="Bot Runner UUID - waits for assignment via Redis")

    def handle(self, *args, **options):
        bot_id = options.get("botid")
        bot_runner_uuid = options.get("botrunneruuid")

        if bot_id and bot_runner_uuid:
            raise ValueError("Cannot specify both --botid and --botrunneruuid")

        if not bot_id and not bot_runner_uuid:
            raise ValueError("Must specify either --botid or --botrunneruuid")

        if bot_runner_uuid:
            bot_id = self._wait_for_bot_assignment(bot_runner_uuid)
            if bot_id is None:
                logger.info("Bot runner exiting without assignment")
                return

        logger.info("Running run bot task for bot %s...", bot_id)
        result = run_bot.run(bot_id)
        logger.info("Run bot task completed with result: %s", result)

    def _wait_for_bot_assignment(self, bot_runner_uuid: str) -> int | None:
        """
        Subscribe to Redis channel and wait for bot assignment command.

        Returns the assigned bot_id when received, or None if shutdown requested.
        """
        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        redis_url = os.getenv("REDIS_URL") + ("?ssl_cert_reqs=none" if os.getenv("DISABLE_REDIS_SSL") else "")
        redis_client = redis.from_url(redis_url)
        pubsub = redis_client.pubsub()

        channel_name = f"bot_runner_{bot_runner_uuid}"
        pubsub.subscribe(channel_name)

        logger.info("Bot runner %s subscribed to channel %s, waiting for assignment...", bot_runner_uuid, channel_name)

        try:
            while self._keep_running:
                message = pubsub.get_message(timeout=1.0)
                if message is None:
                    continue

                if message["type"] != "message":
                    continue

                try:
                    data = json.loads(message["data"].decode("utf-8"))
                except (json.JSONDecodeError, AttributeError) as e:
                    logger.warning("Failed to parse message: %s", e)
                    continue

                command = data.get("command")

                if command == "assign":
                    bot_id = data.get("bot_id")
                    if bot_id is None:
                        logger.warning("Received assign command without bot_id")
                        continue

                    logger.info("Bot runner %s assigned to bot %s", bot_runner_uuid, bot_id)
                    return bot_id
                else:
                    logger.warning("Unknown command received: %s", command)

        finally:
            pubsub.unsubscribe(channel_name)
            pubsub.close()
            redis_client.close()

        return None
