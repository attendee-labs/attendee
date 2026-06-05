from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.cleanup_utils import cleanup_old_audio_chunks


class Command(BaseCommand):
    help = "Deletes AudioChunk rows whose own created_at is more than N days ago. Keyset-paginates AudioChunk by id and deletes in fixed-size batches; both memory footprint and per-transaction size stay constant regardless of the candidate population. Note: the boundary may fall mid-recording, in which case the recording's later chunks will be deleted by the next cron run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete audio chunks created more than this many days ago (default: 30).",
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
            help="Rows deleted per transaction (default: 100). Smaller batches keep transactions short and reduce lock duration / WAL bursts.",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        cleanup_old_audio_chunks(cutoff=cutoff, batch_size=options["batch_size"], dry_run=options["dry_run"])
