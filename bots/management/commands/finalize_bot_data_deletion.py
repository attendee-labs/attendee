import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import Bot, BotStates

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Finalize data deletion for recently updated bots in the DATA_DELETED state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=24,
            help="Only consider bots updated within this many hours.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Number of bots to process per batch.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many bots would be processed without deleting anything.",
        )

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(hours=options["lookback_hours"])
        candidates = Bot.objects.filter(state=BotStates.DATA_DELETED, updated_at__gte=since)
        logger.info(f"Finding data-deleted bots updated since {since.isoformat()}...")

        if options["dry_run"]:
            total = candidates.count()
            logger.info(f"[DRY RUN] Would ensure data is deleted for {total} bots.")
            return

        last_id = 0
        total_processed = 0
        while True:
            bots = list(candidates.filter(id__gt=last_id).order_by("id")[: options["batch_size"]])
            if not bots:
                break

            # Advance past every row in this batch, including failures. Failed
            # bots are retried on the next command run.
            last_id = bots[-1].id
            for bot in bots:
                try:
                    bot.ensure_data_deleted()
                    total_processed += 1
                except Exception:
                    logger.exception(f"Failed to ensure data is deleted for bot {bot.object_id}; will retry next run.")

            logger.info(f"Ensured data is deleted for {total_processed} bots so far.")

        logger.info(f"Finalized data deletion for {total_processed} bots.")
