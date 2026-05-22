import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import Bot, Utterance

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Deletes Utterance rows (transcript segments) for Bots created more than N days ago. "
        "Mirrors what the public Delete-Bot-Transcript endpoint does, but in bulk over many bots. "
        "Other tables (Bot, Recording, AudioChunk, Participant, etc.) are left intact."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="Delete utterances for bots created more than this many days ago (default: 14).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without making any changes.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help=(
                "Number of bots whose utterances are deleted per transaction (default: 500). "
                "Smaller batches keep transactions short and reduce lock duration."
            ),
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        cutoff = timezone.now() - timedelta(days=days)
        logger.info(
            f"Looking for bots created before {cutoff.isoformat()} ({days} days ago)..."
        )

        old_bot_ids = list(
            Bot.objects.filter(created_at__lt=cutoff).values_list("id", flat=True)
        )
        total_bots = len(old_bot_ids)
        logger.info(f"Found {total_bots} bots older than {days} days.")

        if total_bots == 0:
            logger.info("Nothing to do.")
            return

        if dry_run:
            total_utterances = Utterance.objects.filter(
                recording__bot_id__in=old_bot_ids
            ).count()
            logger.info(
                f"[DRY RUN] Would delete {total_utterances} utterances across {total_bots} bots."
            )
            return

        total_deleted = 0
        bots_processed = 0
        for i in range(0, total_bots, batch_size):
            chunk = old_bot_ids[i : i + batch_size]
            deleted_count, _ = Utterance.objects.filter(
                recording__bot_id__in=chunk
            ).delete()
            total_deleted += deleted_count
            bots_processed += len(chunk)
            logger.info(
                f"Processed {bots_processed}/{total_bots} bots; "
                f"deleted {total_deleted} utterances so far."
            )

        logger.info(
            f"Done. Deleted {total_deleted} utterances across {total_bots} bots."
        )
