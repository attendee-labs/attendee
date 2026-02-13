import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import AudioChunk, Recording

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deletes old audio chunks to prevent them from filling up the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete audio chunks for recordings older than this many days (default: 30)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only show how many audio chunks would be deleted without actually deleting them",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]

        cutoff_date = timezone.now() - timedelta(days=days)

        logger.info(f"Finding recordings older than {days} days (before {cutoff_date.isoformat()})...")

        old_recordings = Recording.objects.filter(created_at__lt=cutoff_date, audio_chunks__isnull=False).distinct()

        if dry_run:
            total_chunks = AudioChunk.objects.filter(recording__created_at__lt=cutoff_date).count()
            logger.info(f"[DRY RUN] Would delete {total_chunks} audio chunks from {old_recordings.count()} recordings.")
            return

        total_deleted = 0
        for recording in old_recordings:
            deleted_count, _ = recording.audio_chunks.all().delete()
            if deleted_count > 0:
                total_deleted += deleted_count
                logger.info(f"Deleted {deleted_count} audio chunks from recording {recording.id}")

        logger.info(f"Audio chunk cleanup completed. Deleted {total_deleted} audio chunks.")

