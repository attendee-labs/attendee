"""
Bounded-keyset cleanup helpers for historical row deletion.

Each function deletes rows from one table whose own `created_at` is older
than `cutoff`. We deliberately filter on the target row's own timestamp
(not e.g. Recording.created_at for audio chunks or the parent bot's
ENDED-event timestamp for utterances) because:

  * scheduled bots can have a parent Recording created weeks before the
    bot actually runs, so filtering by `recording.created_at` would
    delete data that is actually recent;
  * filtering on the row's own `created_at` keeps every function as a
    one-line predicate with no joins or subqueries -- simpler to read,
    simpler to index, and bounded purely by the keyset.

A cron run may delete only some of a Recording's audio chunks (or only
some of a meeting's utterances) when the boundary falls mid-recording;
the next run picks up the rest. As long as the cutoff has a reasonable
buffer this is harmless.

The implementation pattern is the same across all three: keyset-paginate
the target table by id, load exactly batch_size ids per iteration,
DELETE by id__in=<those ids>. Both memory footprint and per-transaction
DELETE size stay constant regardless of the candidate population.

These functions are the single source of truth for the deletion logic;
they are called from:

  * bots/management/commands/delete_old_bot_transcripts.py
  * bots/management/commands/delete_old_audio_chunks.py
  * bots/management/commands/cleanup_old_data.py
"""

import logging

from bots.models import AudioChunk, BotDebugScreenshot, BotResourceSnapshot, BotStates, Utterance

logger = logging.getLogger(__name__)


def cleanup_debug_screenshots_for_deleted_bots(*, since, batch_size, dry_run):
    """
    Delete BotDebugScreenshot rows (and their underlying files) that belong to
    bots in the DATA_DELETED state.

    These rows exist because a bot pod can upload a debug screenshot in the
    window between Bot.delete_data() running and the pod being hard-killed.
    DATA_DELETED is a terminal state, so any screenshot attached to such a bot
    is garbage by definition; this sweep makes delete_data() eventually
    consistent without any locking.

    Also unlike the other helpers, rows are fetched as objects rather than
    bare ids, because each row's storage blob must be deleted first. If a
    blob deletion fails, the row is skipped (and retried on the next run)
    so we never orphan a file.

    Returns the number of screenshots deleted (or, for dry_run=True, the
    number that would be deleted).
    """
    logger.info(f"[debug_screenshots] Finding debug screenshots for data-deleted bots active since {since.isoformat()}...")

    candidates = BotDebugScreenshot.objects.filter(
        bot_event__bot__state=BotStates.DATA_DELETED,
        bot_event__bot__last_heartbeat_timestamp__gte=int(since.timestamp()),
    )

    if dry_run:
        total = candidates.count()
        logger.info(f"[debug_screenshots] [DRY RUN] Would delete {total} debug screenshots.")
        return total

    last_id = 0
    total_deleted = 0
    while True:
        screenshots = list(candidates.filter(id__gt=last_id).order_by("id")[:batch_size])
        if not screenshots:
            break

        # Advance past every row in this batch, including any we skip below.
        # Skipped rows are retried on the next cron run rather than looping forever here.
        last_id = screenshots[-1].id

        deletable_ids = []
        for screenshot in screenshots:
            try:
                if screenshot.file and screenshot.file.name:
                    screenshot.file.delete()
                deletable_ids.append(screenshot.id)
            except Exception:
                # Leave the row in place so the file is retried on the next run.
                logger.exception(f"[debug_screenshots] Failed to delete file for screenshot {screenshot.object_id}, will retry next run.")

        deleted, _ = BotDebugScreenshot.objects.filter(id__in=deletable_ids).delete()
        total_deleted += deleted
        logger.info(f"[debug_screenshots] Deleted {total_deleted} debug screenshots so far.")

    logger.info(f"[debug_screenshots] Done. Deleted {total_deleted} debug screenshots.")
    return total_deleted


def cleanup_old_utterances(*, cutoff, batch_size, dry_run):
    """
    Delete Utterance rows whose own created_at is before `cutoff`.

    Returns the number of utterances deleted (or, for dry_run=True, the number
    that would be deleted).
    """
    logger.info(f"[utterances] Finding utterances created before {cutoff.isoformat()}...")

    if dry_run:
        total = Utterance.objects.filter(created_at__lt=cutoff).count()
        logger.info(f"[utterances] [DRY RUN] Would delete {total} utterances.")
        return total

    last_id = 0
    total_deleted = 0
    while True:
        ids = list(Utterance.objects.filter(created_at__lt=cutoff, id__gt=last_id).order_by("id").values_list("id", flat=True)[:batch_size])
        if not ids:
            break

        deleted, _ = Utterance.objects.filter(id__in=ids).delete()
        total_deleted += deleted
        last_id = ids[-1]
        logger.info(f"[utterances] Deleted {total_deleted} utterances so far.")

    logger.info(f"[utterances] Done. Deleted {total_deleted} utterances.")
    return total_deleted


def cleanup_old_audio_chunks(*, cutoff, batch_size, dry_run):
    """
    Delete AudioChunk rows whose own created_at is before `cutoff`.

    Returns the number of audio chunks deleted (or, for dry_run=True, the
    number that would be deleted).
    """
    logger.info(f"[audio_chunks] Finding audio chunks created before {cutoff.isoformat()}...")

    if dry_run:
        total = AudioChunk.objects.filter(created_at__lt=cutoff).count()
        logger.info(f"[audio_chunks] [DRY RUN] Would delete {total} audio chunks.")
        return total

    last_id = 0
    total_deleted = 0
    while True:
        ids = list(AudioChunk.objects.filter(created_at__lt=cutoff, id__gt=last_id).order_by("id").values_list("id", flat=True)[:batch_size])
        if not ids:
            break

        deleted, _ = AudioChunk.objects.filter(id__in=ids).delete()
        total_deleted += deleted
        last_id = ids[-1]
        logger.info(f"[audio_chunks] Deleted {total_deleted} audio chunks so far.")

    logger.info(f"[audio_chunks] Done. Deleted {total_deleted} audio chunks.")
    return total_deleted


def cleanup_old_bot_resource_snapshots(*, cutoff, batch_size, dry_run):
    """
    Delete BotResourceSnapshot rows whose own created_at is before `cutoff`.

    Returns the number of snapshots deleted (or, for dry_run=True, the number
    that would be deleted).
    """
    logger.info(f"[snapshots] Finding bot resource snapshots created before {cutoff.isoformat()}...")

    if dry_run:
        total = BotResourceSnapshot.objects.filter(created_at__lt=cutoff).count()
        logger.info(f"[snapshots] [DRY RUN] Would delete {total} snapshots.")
        return total

    last_id = 0
    total_deleted = 0
    while True:
        ids = list(BotResourceSnapshot.objects.filter(created_at__lt=cutoff, id__gt=last_id).order_by("id").values_list("id", flat=True)[:batch_size])
        if not ids:
            break

        deleted, _ = BotResourceSnapshot.objects.filter(id__in=ids).delete()
        total_deleted += deleted
        last_id = ids[-1]
        logger.info(f"[snapshots] Deleted {total_deleted} snapshots so far.")

    logger.info(f"[snapshots] Done. Deleted {total_deleted} snapshots.")
    return total_deleted
