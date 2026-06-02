import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from django.utils import timezone

from bots.models import AudioChunk, Recording

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deletes AudioChunk rows for Recordings older than N days. Batches deletes by recording id so the command scales as the recording population grows; the previous per-recording loop materialized the full QuerySet in memory and OOMed in pods with modest memory limits."

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

        # Restrict the candidate set to recordings that still have at least one
        # audio chunk. Without this, the cron re-scans every recording older
        # than N days (most already cleared by prior runs), which grows
        # unboundedly with the recording population.
        has_audio_chunks = AudioChunk.objects.filter(recording_id=OuterRef("pk"))
        old_recording_ids = list(Recording.objects.filter(created_at__lt=cutoff).filter(Exists(has_audio_chunks)).values_list("id", flat=True))
        total_recordings = len(old_recording_ids)
        logger.info(f"Found {total_recordings} recordings older than {days} days with audio chunks remaining.")

        if total_recordings == 0:
            logger.info("Nothing to do.")
            return

        if dry_run:
            total_chunks = AudioChunk.objects.filter(recording_id__in=old_recording_ids).count()
            logger.info(f"[DRY RUN] Would delete {total_chunks} audio chunks across {total_recordings} recordings.")
            return

        total_deleted = 0
        recordings_processed = 0
        for i in range(0, total_recordings, batch_size):
            chunk = old_recording_ids[i : i + batch_size]
            deleted_count, _ = AudioChunk.objects.filter(recording_id__in=chunk).delete()
            total_deleted += deleted_count
            recordings_processed += len(chunk)
            logger.info(f"Processed {recordings_processed}/{total_recordings} recordings; deleted {total_deleted} audio chunks so far.")

        logger.info(f"Done. Deleted {total_deleted} audio chunks across {total_recordings} recordings.")
