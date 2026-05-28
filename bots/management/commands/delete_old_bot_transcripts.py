import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import BotEvent, BotStates, Utterance

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deletes Utterance rows (transcript segments) for Bots whose meetings ended more than N days ago. Bot age is determined by the timestamp of the BotEvent transitioning the bot to ENDED, not Bot.created_at, since scheduled bots can be created long before they run. Mirrors what the public Delete-Bot-Transcript endpoint does, but in bulk over many bots. Other tables (Bot, Recording, AudioChunk, Participant, etc.) are left intact."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="Delete utterances for bots that ended more than this many days ago (default: 14).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without making any changes.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help=("Number of bots whose utterances are deleted per transaction (default: 100). Smaller batches keep transactions short and reduce lock duration."),
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        cutoff = timezone.now() - timedelta(days=days)
        logger.info(f"Looking for bots whose ENDED event was logged before {cutoff.isoformat()} ({days} days ago)...")

        old_bot_ids = list(
            BotEvent.objects.filter(
                new_state=BotStates.ENDED,
                created_at__lt=cutoff,
            )
            .values_list("bot_id", flat=True)
            .distinct()
        )
        total_bots = len(old_bot_ids)
        logger.info(f"Found {total_bots} bots that ended more than {days} days ago.")

        if total_bots == 0:
            logger.info("Nothing to do.")
            return

        if dry_run:
            total_utterances = Utterance.objects.filter(recording__bot_id__in=old_bot_ids).count()
            logger.info(f"[DRY RUN] Would delete {total_utterances} utterances across {total_bots} bots.")
            return

        total_deleted = 0
        bots_processed = 0
        for i in range(0, total_bots, batch_size):
            chunk = old_bot_ids[i : i + batch_size]
            deleted_count, _ = Utterance.objects.filter(recording__bot_id__in=chunk).delete()
            total_deleted += deleted_count
            bots_processed += len(chunk)
            logger.info(f"Processed {bots_processed}/{total_bots} bots; deleted {total_deleted} utterances so far.")

        logger.info(f"Done. Deleted {total_deleted} utterances across {total_bots} bots.")
