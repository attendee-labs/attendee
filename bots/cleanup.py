"""
Bounded-keyset cleanup helpers for historical row deletion.

Each function deletes rows from one table whose age (per that table's age
semantics) is older than `cutoff`. The implementation pattern is the same
across all three: keyset-paginate the target table by id, load exactly
batch_size ids per iteration, DELETE by id__in=<those ids>. Both memory
footprint and per-transaction DELETE size stay constant regardless of the
candidate population or any per-parent fan-out (one Recording owning
hundreds of thousands of AudioChunks, one Bot owning millions of
Utterances).

These functions are the single source of truth for the deletion logic;
they are called from:

  * bots/management/commands/delete_old_bot_transcripts.py
  * bots/management/commands/delete_old_audio_chunks.py
  * bots/management/commands/cleanup_old_data.py
"""

import logging

from django.db.models import Exists, OuterRef

from bots.models import AudioChunk, BotEvent, BotResourceSnapshot, BotStates, Utterance

logger = logging.getLogger(__name__)


def cleanup_old_utterances(*, cutoff, batch_size, dry_run):
    """
    Delete Utterance rows for Bots whose ENDED BotEvent was logged before `cutoff`.

    Bot age is determined by the BotEvent transitioning the bot to ENDED, not
    Bot.created_at, since scheduled bots can be created long before they run.
    Mirrors what the public Delete-Bot-Transcript endpoint does, but in bulk.
    Other tables (Bot, Recording, AudioChunk, Participant, etc.) are left intact.

    Returns the number of utterances deleted (or, for dry_run=True, the number
    that would be deleted).
    """
    logger.info(f"[utterances] Finding utterances for bots that ENDED before {cutoff.isoformat()}...")

    bot_ended_before_cutoff = BotEvent.objects.filter(
        new_state=BotStates.ENDED,
        created_at__lt=cutoff,
        bot_id=OuterRef("recording__bot_id"),
    )

    if dry_run:
        total = Utterance.objects.filter(Exists(bot_ended_before_cutoff)).count()
        logger.info(f"[utterances] [DRY RUN] Would delete {total} utterances.")
        return total

    last_id = 0
    total_deleted = 0
    while True:
        ids = list(Utterance.objects.filter(Exists(bot_ended_before_cutoff), id__gt=last_id).order_by("id").values_list("id", flat=True)[:batch_size])
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
    Delete AudioChunk rows whose parent Recording was created before `cutoff`.

    Returns the number of audio chunks deleted (or, for dry_run=True, the
    number that would be deleted).
    """
    logger.info(f"[audio_chunks] Finding audio chunks for recordings created before {cutoff.isoformat()}...")

    if dry_run:
        total = AudioChunk.objects.filter(recording__created_at__lt=cutoff).count()
        logger.info(f"[audio_chunks] [DRY RUN] Would delete {total} audio chunks.")
        return total

    last_id = 0
    total_deleted = 0
    while True:
        ids = list(AudioChunk.objects.filter(recording__created_at__lt=cutoff, id__gt=last_id).order_by("id").values_list("id", flat=True)[:batch_size])
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
    Delete BotResourceSnapshot rows created before `cutoff`.

    Snapshot age comes from the snapshot's own created_at; no join needed.

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
