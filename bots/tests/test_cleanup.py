from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization
from bots.cleanup_utils import cleanup_old_audio_chunks, cleanup_old_bot_resource_snapshots, cleanup_old_utterances
from bots.models import AudioChunk, Bot, BotResourceSnapshot, BotStates, Participant, Project, Recording, RecordingStates, Utterance


def _backdate(model, pk, when):
    """auto_now_add=True ignores assignments at create time; use update() to override created_at in tests."""
    model.objects.filter(pk=pk).update(created_at=when)


class CleanupFixturesMixin:
    """Org/project/bot/recording/participant for the rest of the file, plus per-model factories that let the test pin created_at."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Cleanup Test Org")
        self.project = Project.objects.create(organization=self.organization, name="Cleanup Test Project")
        self.bot = Bot.objects.create(project=self.project, name="Cleanup Bot", meeting_url="https://test.com/meeting", state=BotStates.ENDED)
        self.participant = Participant.objects.create(bot=self.bot, uuid="cleanup-participant", full_name="P")
        self.recording = Recording.objects.create(bot=self.bot, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)
        self.now = timezone.now()
        self.cutoff = self.now - timedelta(days=30)

    def _make_utterance(self, created_at):
        u = Utterance.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"x", timestamp_ms=0, duration_ms=10)
        _backdate(Utterance, u.pk, created_at)
        u.refresh_from_db()
        return u

    def _make_audio_chunk(self, created_at):
        c = AudioChunk.objects.create(recording=self.recording, participant=self.participant, audio_blob=b"x", timestamp_ms=0, duration_ms=10, sample_rate=16000)
        _backdate(AudioChunk, c.pk, created_at)
        c.refresh_from_db()
        return c

    def _make_snapshot(self, created_at):
        s = BotResourceSnapshot.objects.create(bot=self.bot, data={})
        _backdate(BotResourceSnapshot, s.pk, created_at)
        s.refresh_from_db()
        return s


# -------------------------------------------------------------------------
# bots.cleanup_utils helpers
# -------------------------------------------------------------------------


class CleanupOldUtterancesTestCase(CleanupFixturesMixin, TestCase):
    def test_deletes_old_keeps_new(self):
        old = self._make_utterance(self.now - timedelta(days=60))
        new = self._make_utterance(self.now - timedelta(days=1))

        deleted = cleanup_old_utterances(cutoff=self.cutoff, batch_size=10, dry_run=False)

        self.assertEqual(deleted, 1)
        self.assertFalse(Utterance.objects.filter(pk=old.pk).exists())
        self.assertTrue(Utterance.objects.filter(pk=new.pk).exists())

    def test_dry_run_reports_count_but_does_not_delete(self):
        old = self._make_utterance(self.now - timedelta(days=60))

        reported = cleanup_old_utterances(cutoff=self.cutoff, batch_size=10, dry_run=True)

        self.assertEqual(reported, 1)
        self.assertTrue(Utterance.objects.filter(pk=old.pk).exists())

    def test_empty_table_returns_zero(self):
        self.assertEqual(cleanup_old_utterances(cutoff=self.cutoff, batch_size=10, dry_run=False), 0)

    def test_keyset_loops_until_drained(self):
        # 7 old rows with batch_size=3 forces 3 iterations (3, 3, 1) plus a final empty iteration to terminate.
        # If the keyset cursor did not advance, only the first 3 rows would be deleted.
        for _ in range(7):
            self._make_utterance(self.now - timedelta(days=60))
        for _ in range(2):
            self._make_utterance(self.now - timedelta(days=1))

        deleted = cleanup_old_utterances(cutoff=self.cutoff, batch_size=3, dry_run=False)

        self.assertEqual(deleted, 7)
        self.assertEqual(Utterance.objects.count(), 2)

    def test_second_run_is_a_noop(self):
        self._make_utterance(self.now - timedelta(days=60))
        cleanup_old_utterances(cutoff=self.cutoff, batch_size=10, dry_run=False)

        self.assertEqual(cleanup_old_utterances(cutoff=self.cutoff, batch_size=10, dry_run=False), 0)


class CleanupOldAudioChunksTestCase(CleanupFixturesMixin, TestCase):
    def test_deletes_old_keeps_new(self):
        old = self._make_audio_chunk(self.now - timedelta(days=60))
        new = self._make_audio_chunk(self.now - timedelta(days=1))

        deleted = cleanup_old_audio_chunks(cutoff=self.cutoff, batch_size=10, dry_run=False)

        self.assertEqual(deleted, 1)
        self.assertFalse(AudioChunk.objects.filter(pk=old.pk).exists())
        self.assertTrue(AudioChunk.objects.filter(pk=new.pk).exists())

    def test_dry_run_reports_count_but_does_not_delete(self):
        old = self._make_audio_chunk(self.now - timedelta(days=60))

        reported = cleanup_old_audio_chunks(cutoff=self.cutoff, batch_size=10, dry_run=True)

        self.assertEqual(reported, 1)
        self.assertTrue(AudioChunk.objects.filter(pk=old.pk).exists())

    def test_empty_table_returns_zero(self):
        self.assertEqual(cleanup_old_audio_chunks(cutoff=self.cutoff, batch_size=10, dry_run=False), 0)

    def test_keyset_loops_until_drained(self):
        for _ in range(7):
            self._make_audio_chunk(self.now - timedelta(days=60))
        for _ in range(2):
            self._make_audio_chunk(self.now - timedelta(days=1))

        deleted = cleanup_old_audio_chunks(cutoff=self.cutoff, batch_size=3, dry_run=False)

        self.assertEqual(deleted, 7)
        self.assertEqual(AudioChunk.objects.count(), 2)

    def test_second_run_is_a_noop(self):
        self._make_audio_chunk(self.now - timedelta(days=60))
        cleanup_old_audio_chunks(cutoff=self.cutoff, batch_size=10, dry_run=False)

        self.assertEqual(cleanup_old_audio_chunks(cutoff=self.cutoff, batch_size=10, dry_run=False), 0)


class CleanupOldBotResourceSnapshotsTestCase(CleanupFixturesMixin, TestCase):
    def test_deletes_old_keeps_new(self):
        old = self._make_snapshot(self.now - timedelta(days=60))
        new = self._make_snapshot(self.now - timedelta(days=1))

        deleted = cleanup_old_bot_resource_snapshots(cutoff=self.cutoff, batch_size=10, dry_run=False)

        self.assertEqual(deleted, 1)
        self.assertFalse(BotResourceSnapshot.objects.filter(pk=old.pk).exists())
        self.assertTrue(BotResourceSnapshot.objects.filter(pk=new.pk).exists())

    def test_dry_run_reports_count_but_does_not_delete(self):
        old = self._make_snapshot(self.now - timedelta(days=60))

        reported = cleanup_old_bot_resource_snapshots(cutoff=self.cutoff, batch_size=10, dry_run=True)

        self.assertEqual(reported, 1)
        self.assertTrue(BotResourceSnapshot.objects.filter(pk=old.pk).exists())

    def test_empty_table_returns_zero(self):
        self.assertEqual(cleanup_old_bot_resource_snapshots(cutoff=self.cutoff, batch_size=10, dry_run=False), 0)

    def test_keyset_loops_until_drained(self):
        for _ in range(7):
            self._make_snapshot(self.now - timedelta(days=60))
        for _ in range(2):
            self._make_snapshot(self.now - timedelta(days=1))

        deleted = cleanup_old_bot_resource_snapshots(cutoff=self.cutoff, batch_size=3, dry_run=False)

        self.assertEqual(deleted, 7)
        self.assertEqual(BotResourceSnapshot.objects.count(), 2)


# -------------------------------------------------------------------------
# Management commands
# -------------------------------------------------------------------------


class CleanupOldDataCommandTestCase(CleanupFixturesMixin, TestCase):
    def test_tables_subset_only_touches_that_table(self):
        old_utterance = self._make_utterance(self.now - timedelta(days=60))
        old_chunk = self._make_audio_chunk(self.now - timedelta(days=60))
        old_snapshot = self._make_snapshot(self.now - timedelta(days=60))

        call_command("cleanup_old_data", "--days=30", "--batch-size=10", "--tables=utterances")

        self.assertFalse(Utterance.objects.filter(pk=old_utterance.pk).exists())
        self.assertTrue(AudioChunk.objects.filter(pk=old_chunk.pk).exists())
        self.assertTrue(BotResourceSnapshot.objects.filter(pk=old_snapshot.pk).exists())

    def test_default_runs_all_three_tables(self):
        old_utterance = self._make_utterance(self.now - timedelta(days=60))
        old_chunk = self._make_audio_chunk(self.now - timedelta(days=60))
        old_snapshot = self._make_snapshot(self.now - timedelta(days=60))

        call_command("cleanup_old_data", "--days=30", "--batch-size=10")

        self.assertFalse(Utterance.objects.filter(pk=old_utterance.pk).exists())
        self.assertFalse(AudioChunk.objects.filter(pk=old_chunk.pk).exists())
        self.assertFalse(BotResourceSnapshot.objects.filter(pk=old_snapshot.pk).exists())

    def test_unknown_table_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("cleanup_old_data", "--tables=bogus")

    def test_dry_run_does_not_delete(self):
        old = self._make_utterance(self.now - timedelta(days=60))

        call_command("cleanup_old_data", "--days=30", "--batch-size=10", "--dry-run")

        self.assertTrue(Utterance.objects.filter(pk=old.pk).exists())

    def test_failure_in_one_table_does_not_stop_the_others(self):
        # Old rows in two tables; force the audio_chunks function to blow up.
        # The dispatcher should still process the utterances table successfully.
        old_utterance = self._make_utterance(self.now - timedelta(days=60))
        old_chunk = self._make_audio_chunk(self.now - timedelta(days=60))

        boom = MagicMock(side_effect=RuntimeError("simulated failure"))
        with patch.dict("bots.management.commands.cleanup_old_data.TABLE_TO_CLEANUP", {"audio_chunks": boom}):
            call_command("cleanup_old_data", "--days=30", "--batch-size=10", "--tables=utterances,audio_chunks")

        boom.assert_called_once()
        self.assertFalse(Utterance.objects.filter(pk=old_utterance.pk).exists())
        self.assertTrue(AudioChunk.objects.filter(pk=old_chunk.pk).exists())


class DeleteOldBotTranscriptsCommandTestCase(CleanupFixturesMixin, TestCase):
    def test_command_calls_shared_function(self):
        old = self._make_utterance(self.now - timedelta(days=60))
        new = self._make_utterance(self.now - timedelta(days=1))

        call_command("delete_old_bot_transcripts", "--days=30", "--batch-size=10")

        self.assertFalse(Utterance.objects.filter(pk=old.pk).exists())
        self.assertTrue(Utterance.objects.filter(pk=new.pk).exists())


class DeleteOldAudioChunksCommandTestCase(CleanupFixturesMixin, TestCase):
    def test_command_calls_shared_function(self):
        old = self._make_audio_chunk(self.now - timedelta(days=60))
        new = self._make_audio_chunk(self.now - timedelta(days=1))

        call_command("delete_old_audio_chunks", "--days=30", "--batch-size=10")

        self.assertFalse(AudioChunk.objects.filter(pk=old.pk).exists())
        self.assertTrue(AudioChunk.objects.filter(pk=new.pk).exists())
