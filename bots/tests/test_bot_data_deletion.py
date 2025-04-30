from unittest.mock import PropertyMock, patch

from django.core.files.base import ContentFile
from django.test import TransactionTestCase
from django.test.utils import override_settings

from bots.models import Bot, BotEventTypes, BotStates, Organization, Participant, Project, Recording, RecordingStates, Utterance


def mock_file_field_delete_sets_name_to_none(instance, save=True):
    """
    A side_effect function for mocking FieldFile.delete.
    Sets the FieldFile's name to None and saves the parent model instance.
    """
    # 'instance' here is the FieldFile instance being deleted
    instance.name = None
    if save:
        # instance.instance refers to the model instance (e.g., Recording)
        # that owns this FieldFile.
        instance.instance.save()


def mock_file_field_save(instance, name, content, save=True):
    """
    A side_effect function for mocking FieldFile.save.
    Sets the FieldFile's name to the provided name and saves the parent model instance.
    """
    # 'instance' here is the FieldFile instance being saved
    instance.name = name
    if save:
        # instance.instance refers to the model instance (e.g., Recording)
        # that owns this FieldFile.
        instance.instance.save()


class TestBotDataDeletion(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings_override = override_settings(AWS_RECORDING_STORAGE_BUCKET_NAME="test-bucket")
        cls.settings_override.enable()

    def setUp(self):
        # Setup patches
        self.bucket_mock_patch = patch("storages.backends.s3boto3.S3Boto3Storage.bucket", new_callable=PropertyMock)
        self.delete_patch = patch("django.db.models.fields.files.FieldFile.delete", autospec=True)
        self.save_patch = patch("django.db.models.fields.files.FieldFile.save", autospec=True)

        # Start patches
        self.bucket_mock = self.bucket_mock_patch.start()
        self.delete_mock = self.delete_patch.start()
        self.save_mock = self.save_patch.start()

        # Set side effects
        self.delete_mock.side_effect = mock_file_field_delete_sets_name_to_none
        self.save_mock.side_effect = mock_file_field_save

        # Create test organization
        self.organization = Organization.objects.create(name="Test Org")

        # Create test project
        self.project = Project.objects.create(organization=self.organization, name="Test Project")

        # Create two test bots
        self.bot1 = Bot.objects.create(project=self.project, name="Bot One", meeting_url="https://test.com/meeting1", state=BotStates.ENDED)

        self.bot2 = Bot.objects.create(project=self.project, name="Bot Two", meeting_url="https://test.com/meeting2", state=BotStates.ENDED)

        # Create participants for each bot
        self.participant1 = Participant.objects.create(bot=self.bot1, uuid="participant1", full_name="Test Participant 1")

        self.participant2 = Participant.objects.create(bot=self.bot2, uuid="participant2", full_name="Test Participant 2")

        # Create recordings for each bot
        self.recording1 = Recording.objects.create(bot=self.bot1, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)
        # Add a file to the recording
        self.recording1.file.save("test1.mp4", ContentFile(b"test content 1"))

        self.recording2 = Recording.objects.create(bot=self.bot2, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)
        # Add a file to the recording
        self.recording2.file.save("test2.mp4", ContentFile(b"test content 2"))

        # Create utterances for each recording
        self.utterance1 = Utterance.objects.create(recording=self.recording1, participant=self.participant1, audio_blob=b"test audio 1", timestamp_ms=1000, duration_ms=500)

        self.utterance2 = Utterance.objects.create(recording=self.recording2, participant=self.participant2, audio_blob=b"test audio 2", timestamp_ms=1000, duration_ms=500)

    def tearDown(self):
        # Stop all patches
        self.save_patch.stop()
        self.delete_patch.stop()
        self.bucket_mock_patch.stop()

    def test_delete_data_deletes_specific_bot_data_only(self):
        """Test that deleting data for one bot doesn't affect other bots"""
        # Verify initial state
        self.assertEqual(Bot.objects.count(), 2)
        self.assertEqual(Participant.objects.count(), 2)
        self.assertEqual(Recording.objects.count(), 2)
        self.assertEqual(Utterance.objects.count(), 2)

        # Delete data for bot1
        self.bot1.delete_data()

        # Verify bot1's data is deleted
        self.assertEqual(Participant.objects.filter(bot=self.bot1).count(), 0)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 0)

        # Verify bot2's data is still intact
        self.assertEqual(Participant.objects.filter(bot=self.bot2).count(), 1)
        self.assertEqual(Recording.objects.filter(bot=self.bot2).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot2).count(), 1)

        # Verify bot2's state is still ENDED
        self.bot2.refresh_from_db()
        self.assertEqual(self.bot2.state, BotStates.ENDED)

        # Verify bot1's state changed to DATA_DELETED
        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)

        # Verify event was created
        event = self.bot1.bot_events.filter(event_type=BotEventTypes.DATA_DELETED).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.old_state, BotStates.ENDED)
        self.assertEqual(event.new_state, BotStates.DATA_DELETED)

    def test_delete_data_invalid_state(self):
        """Test that delete_data raises an error if bot is not in a valid state"""
        # Change bot state to one that's not valid for data deletion
        self.bot1.state = BotStates.JOINED_RECORDING
        self.bot1.save()

        # Verify delete_data raises ValueError
        with self.assertRaises(ValueError):
            self.bot1.delete_data()

        # Verify no data was deleted
        self.assertEqual(Participant.objects.filter(bot=self.bot1).count(), 1)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 1)

    def test_delete_data_multiple_recordings(self):
        """Test that delete_data deletes data from multiple recordings"""
        # Create another recording for bot1
        recording1b = Recording.objects.create(bot=self.bot1, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)
        recording1b.file.save("test1b.mp4", ContentFile(b"test content 1b"))

        # Create utterance for the new recording
        Utterance.objects.create(recording=recording1b, participant=self.participant1, audio_blob=b"test audio 1b", timestamp_ms=2000, duration_ms=500)

        # Initial count
        self.assertEqual(Recording.objects.filter(bot=self.bot1).count(), 2)
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 2)

        # Delete data for bot1
        self.bot1.delete_data()

        # Verify all of bot1's data is deleted
        self.assertEqual(Utterance.objects.filter(recording__bot=self.bot1).count(), 0)

        # Verify recording files are deleted but records still exist
        self.bot1.refresh_from_db()
        self.assertEqual(Recording.objects.filter(bot=self.bot1).count(), 2)

        # Check files are deleted
        for recording in Recording.objects.filter(bot=self.bot1):
            self.assertFalse(recording.file)

    def test_fatal_error_to_data_deleted_transition(self):
        """Test that a bot in FATAL_ERROR state can transition to DATA_DELETED"""
        # Change bot state to FATAL_ERROR
        self.bot1.state = BotStates.FATAL_ERROR
        self.bot1.save()

        # Delete data
        self.bot1.delete_data()

        # Verify state changed to DATA_DELETED
        self.bot1.refresh_from_db()
        self.assertEqual(self.bot1.state, BotStates.DATA_DELETED)
