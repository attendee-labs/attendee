import logging
import signal
import time

from django.core.management.base import BaseCommand

from bots.bot_pod_creator.bot_pod_creator import POD_LABELS_FILE_PATH
from bots.models import Bot
from bots.tasks import run_bot

logger = logging.getLogger(__name__)

ASSIGNED_BOT_ID_LABEL = "assigned-bot-id"
UNASSIGNED_LABEL_VALUE = "none"


def read_pod_labels(labels_file_path: str) -> dict[str, str]:
    """
    Parse a downward API labels file. Each line has the form key="value".
    """
    labels = {}
    with open(labels_file_path) as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if not sep:
                continue
            # Label values can only contain alphanumerics, '-', '_' and '.', so stripping the quotes is enough
            labels[key] = value.strip('"')
    return labels


class Command(BaseCommand):
    help = "Waits for the pod's assigned-bot-id label to be set (via a downward API volume), then runs the bot"

    _keep_running = True

    def _graceful_exit(self, signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        self._keep_running = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--labels-file",
            type=str,
            default=POD_LABELS_FILE_PATH,
            help=f"Path to the downward API file containing the pod's labels (default: {POD_LABELS_FILE_PATH})",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=0.25,
            help="Seconds between checks of the labels file (default: 0.25)",
        )

    def handle(self, *args, **options):
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        bot_object_id = self._wait_for_assignment(options["labels_file"], options["poll_interval"])
        if bot_object_id is None:
            logger.info("Bot runner exiting without assignment")
            return

        bot_id = Bot.objects.filter(object_id=bot_object_id).values_list("id", flat=True).first()
        if bot_id is None:
            logger.error("Pod was assigned to unknown bot_object_id=%s, exiting", bot_object_id)
            return

        logger.info("Running run bot task for bot %s (%s)...", bot_object_id, bot_id)
        result = run_bot.run(bot_id)
        logger.info("Run bot task completed with result: %s", result)

    def _wait_for_assignment(self, labels_file_path: str, poll_interval: float) -> str | None:
        """
        Poll the labels file until the assigned-bot-id label is set.

        Returns the assigned bot object id, or None if shutdown was requested.
        """
        logger.info("Bot runner waiting for %s label in %s...", ASSIGNED_BOT_ID_LABEL, labels_file_path)

        while self._keep_running:
            assigned_bot_object_id = read_pod_labels(labels_file_path).get(ASSIGNED_BOT_ID_LABEL)
            if assigned_bot_object_id and assigned_bot_object_id != UNASSIGNED_LABEL_VALUE:
                logger.info("Pod was assigned to bot %s", assigned_bot_object_id)
                return assigned_bot_object_id

            time.sleep(poll_interval)

        return None
