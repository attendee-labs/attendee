from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.cleanup import cleanup_old_utterances


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
            help="Rows deleted per transaction (default: 100). Smaller batches keep transactions short and reduce lock duration.",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        cleanup_old_utterances(cutoff=cutoff, batch_size=options["batch_size"], dry_run=options["dry_run"])
