import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.cleanup_utils import cleanup_debug_screenshots_for_deleted_bots

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete debug screenshots (rows and files) belonging to bots whose data has been deleted."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lookback-days",
            type=int,
            default=7,
            help="Only consider bots whose last heartbeat is within this many days. Stragglers appear within minutes of data deletion, so this is a large buffer.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Number of screenshots to process per batch.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many screenshots would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(days=options["lookback_days"])
        total = cleanup_debug_screenshots_for_deleted_bots(
            since=since,
            batch_size=options["batch_size"],
            dry_run=options["dry_run"],
        )
        logger.info(f"Processed {total} debug screenshots.")
