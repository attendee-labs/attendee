import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import AudioChunk

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deletes AudioChunk rows whose Recording is older than N days. Keyset-paginates AudioChunk by id and deletes in fixed-size batches; both memory footprint and per-transaction size stay constant regardless of the candidate population or how lopsided its distribution across recordings is."

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
            default=1000,
            help="Number of audio chunks deleted per transaction (default: 1000). Smaller batches keep transactions short and reduce lock duration / WAL bursts.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        cutoff = timezone.now() - timedelta(days=days)
        logger.info(f"Finding audio chunks for recordings older than {days} days (before {cutoff.isoformat()})...")

        if dry_run:
            total_chunks = AudioChunk.objects.filter(recording__created_at__lt=cutoff).count()
            logger.info(f"[DRY RUN] Would delete {total_chunks} audio chunks.")
            return

        # Keyset-paginate AudioChunk by id. Each iteration loads at most
        # batch_size ids and issues a single bounded DELETE, so both the
        # SELECT and the DELETE stay small regardless of the candidate
        # population or how many chunks each recording owns.
        last_chunk_id = 0
        total_deleted = 0
        while True:
            chunk_ids = list(AudioChunk.objects.filter(recording__created_at__lt=cutoff, id__gt=last_chunk_id).order_by("id").values_list("id", flat=True)[:batch_size])
            if not chunk_ids:
                break

            deleted_count, _ = AudioChunk.objects.filter(id__in=chunk_ids).delete()
            total_deleted += deleted_count
            last_chunk_id = chunk_ids[-1]
            logger.info(f"Deleted {total_deleted} audio chunks so far.")

        logger.info(f"Done. Deleted {total_deleted} audio chunks.")
