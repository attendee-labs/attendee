import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import BotDebugScreenshot, BotStates

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Finalize data deletion by removing debug screenshots uploaded after a bot entered DATA_DELETED."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lookback-days",
            type=int,
            default=7,
            help="Only consider bots updated within this many days. Stragglers appear within minutes of data deletion, so this is a large buffer.",
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
        candidates = BotDebugScreenshot.objects.filter(
            bot_event__bot__state=BotStates.DATA_DELETED,
            bot_event__bot__updated_at__gte=since,
        )
        logger.info(f"Finding debug screenshots for data-deleted bots updated since {since.isoformat()}...")

        if options["dry_run"]:
            total = candidates.count()
            logger.info(f"[DRY RUN] Would delete {total} debug screenshots.")
            return

        last_id = 0
        total_deleted = 0
        while True:
            screenshots = list(candidates.filter(id__gt=last_id).order_by("id")[: options["batch_size"]])
            if not screenshots:
                break

            # Advance past every row in this batch, including failures. Failed
            # files remain in place and are retried on the next command run.
            last_id = screenshots[-1].id
            deletable_ids = []
            for screenshot in screenshots:
                try:
                    if screenshot.file and screenshot.file.name:
                        screenshot.file.delete()
                    deletable_ids.append(screenshot.id)
                except Exception:
                    logger.exception(f"Failed to delete file for screenshot {screenshot.object_id}; will retry next run.")

            deleted, _ = BotDebugScreenshot.objects.filter(id__in=deletable_ids).delete()
            total_deleted += deleted
            logger.info(f"Deleted {total_deleted} debug screenshots so far.")

        logger.info(f"Finalized deletion of {total_deleted} debug screenshots.")
