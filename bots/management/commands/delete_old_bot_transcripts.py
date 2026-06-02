from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.cleanup_utils import cleanup_old_utterances


class Command(BaseCommand):
    help = "Deletes Utterance rows (transcript segments) whose own created_at is more than N days ago. Other tables (Bot, Recording, AudioChunk, Participant, etc.) are left intact. Note: the boundary may fall mid-meeting, in which case a recording's later utterances will be deleted by the next cron run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=14,
            help="Delete utterances created more than this many days ago (default: 14).",
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
            help="Rows deleted per transaction (default: 100). Smaller batches keep transactions short and reduce lock duration.",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        cleanup_old_utterances(cutoff=cutoff, batch_size=options["batch_size"], dry_run=options["dry_run"])
