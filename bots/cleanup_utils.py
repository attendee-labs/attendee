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

from bots.models import AudioChunk, BotResourceSnapshot, Utterance

logger = logging.getLogger(__name__)


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
