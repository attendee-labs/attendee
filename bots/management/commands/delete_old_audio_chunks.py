import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from django.utils import timezone

from bots.models import AudioChunk, Recording

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deletes AudioChunk rows for Recordings older than N days. Streams candidate recordings via keyset pagination and deletes their audio chunks in fixed-size batches; never materializes the full candidate list, so memory footprint stays flat regardless of how many recordings qualify."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete audio chunks for recordings older than this many days (default: 30).",
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
            help="Number of recordings whose audio chunks are deleted per transaction (default: 100). Smaller batches keep transactions short and reduce lock duration.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        cutoff = timezone.now() - timedelta(days=days)
        logger.info(f"Finding recordings older than {days} days (before {cutoff.isoformat()})...")

        if dry_run:
            total_chunks = AudioChunk.objects.filter(recording__created_at__lt=cutoff).count()
            logger.info(f"[DRY RUN] Would delete {total_chunks} audio chunks for recordings older than {days} days.")
            return

        # Stream candidate recordings via keyset pagination (id > last_id).
        # Each iteration loads at most batch_size ids, so memory stays flat
        # regardless of how many recordings qualify. The Exists subquery
        # keeps the cron's steady-state work proportional to the new slice
        # of recordings rather than the cumulative population.
        has_audio_chunks = AudioChunk.objects.filter(recording_id=OuterRef("pk"))

        last_id = 0
        total_deleted = 0
        recordings_processed = 0
        while True:
            batch_ids = list(Recording.objects.filter(created_at__lt=cutoff, id__gt=last_id).filter(Exists(has_audio_chunks)).order_by("id").values_list("id", flat=True)[:batch_size])
            if not batch_ids:
                break

            deleted_count, _ = AudioChunk.objects.filter(recording_id__in=batch_ids).delete()
            total_deleted += deleted_count
            recordings_processed += len(batch_ids)
            last_id = batch_ids[-1]
            logger.info(f"Processed {recordings_processed} recordings; deleted {total_deleted} audio chunks so far.")

        logger.info(f"Done. Deleted {total_deleted} audio chunks across {recordings_processed} recordings.")
