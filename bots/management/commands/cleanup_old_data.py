import logging
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bots import cleanup

logger = logging.getLogger(__name__)


# Each table key maps to the shared cleanup function in bots/cleanup.py.
# Adding a new table = adding a function there + a new entry here.
TABLE_TO_CLEANUP = {
    "utterances": cleanup.cleanup_old_utterances,
    "audio_chunks": cleanup.cleanup_old_audio_chunks,
    "snapshots": cleanup.cleanup_old_bot_resource_snapshots,
}


class Command(BaseCommand):
    help = "Deletes historical records older than --days for any subset of {utterances, audio_chunks, snapshots}. Dispatches to the shared functions in bots/cleanup.py so this command and the per-table delete_* commands stay in lockstep. A failure in one table does not stop the others; the cron will retry the failed one on the next tick."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Retain records newer than this many days; delete the rest (default: 30).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Rows deleted per transaction (default: 100). Smaller batches keep transactions short and reduce lock duration / WAL bursts.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without making any changes.",
        )
        parser.add_argument(
            "--tables",
            type=str,
            default=",".join(TABLE_TO_CLEANUP.keys()),
            help=f"Comma-separated subset of {list(TABLE_TO_CLEANUP.keys())} to clean up (default: all). Unknown values raise an error.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        tables = [t.strip() for t in options["tables"].split(",") if t.strip()]

        unknown = [t for t in tables if t not in TABLE_TO_CLEANUP]
        if unknown:
            raise CommandError(f"Unknown table(s): {unknown}. Choices: {list(TABLE_TO_CLEANUP.keys())}")

        cutoff = timezone.now() - timedelta(days=days)
        logger.info(f"Cleanup starting: tables={tables} days={days} cutoff={cutoff.isoformat()} batch_size={batch_size} dry_run={dry_run}")

        results = {}
        for table in tables:
            try:
                results[table] = TABLE_TO_CLEANUP[table](cutoff=cutoff, batch_size=batch_size, dry_run=dry_run)
            except Exception:
                # One bad block should not stop the others; the cron will retry on the next tick.
                logger.exception(f"[{table}] Cleanup failed; continuing with the remaining tables.")
                results[table] = "FAILED"

        logger.info(f"Cleanup done. Results: {results}")
