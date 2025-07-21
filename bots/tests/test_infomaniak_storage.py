"""
Tests for the Infomaniak Swift storage backend and utilities.
Tests the migration from AWS S3 to Infomaniak Swift storage.
"""

from unittest.mock import MagicMock, patch

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, TransactionTestCase, override_settings

from bots.models import Bot, BotStates, Organization, Project, Recording, RecordingStates
from bots.storage import InfomaniakSwiftStorage
from bots.storage.infomaniak_swift_utils import (
    delete_file_from_swift,
    download_file_from_swift,
    list_objects_in_container,
    object_exists,
    upload_file_to_swift,
)


class InfomaniakStorageBackendTest(TestCase):
    """Test our Infomaniak Swift storage backend functionality"""

    def test_storage_backend_instantiation(self):
        """Test that we can create our storage backend"""
        storage = InfomaniakSwiftStorage()
        self.assertIsInstance(storage, InfomaniakSwiftStorage)

    def test_default_storage_is_swift(self):
        """Test that default storage is our Swift backend"""
        self.assertIsInstance(default_storage, InfomaniakSwiftStorage)
        self.assertEqual(default_storage.__class__.__name__, "InfomaniakSwiftStorage")

    @override_settings(SWIFT_CONTAINER_MEETS="test-bucket")
    def test_storage_settings(self):
        """Test that storage settings are properly configured"""
        from django.conf import settings

        self.assertEqual(settings.SWIFT_CONTAINER_MEETS, "test-bucket")

        # Test that STORAGES setting uses our backend
        storages_config = getattr(settings, "STORAGES", {})
        default_config = storages_config.get("default", {})
        self.assertEqual(default_config.get("BACKEND"), "bots.storage.InfomaniakSwiftStorage")


class InfomaniakSwiftUtilsTest(TestCase):
    """Test Infomaniak Swift utility functions"""

    @patch("bots.storage.infomaniak_swift_utils.get_swift_client")
    def test_upload_file_to_swift(self, mock_get_client):
        """Test file upload utility"""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        test_content = b"test content"
        test_filename = "test.txt"

        result = upload_file_to_swift(test_content, test_filename)

        self.assertEqual(result, test_filename)
        mock_client.put_object.assert_called_once()

    @patch("bots.storage.infomaniak_swift_utils.get_swift_client")
    def test_download_file_from_swift(self, mock_get_client):
        """Test file download utility"""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        test_content = b"downloaded content"
        mock_client.get_object.return_value = ({}, test_content)

        result = download_file_from_swift("test.txt")

        self.assertEqual(result, test_content)
        mock_client.get_object.assert_called_once()

    @patch("bots.storage.infomaniak_swift_utils.get_swift_client")
    def test_delete_file_from_swift(self, mock_get_client):
        """Test file deletion utility"""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        result = delete_file_from_swift("test.txt")

        self.assertTrue(result)
        mock_client.delete_object.assert_called_once()

    @patch("bots.storage.infomaniak_swift_utils.get_swift_client")
    def test_list_objects_in_container(self, mock_get_client):
        """Test listing objects utility"""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_objects = [
            {"name": "file1.txt"},
            {"name": "file2.txt"},
        ]
        mock_client.get_container.return_value = ({}, mock_objects)

        result = list_objects_in_container()

        self.assertEqual(result, ["file1.txt", "file2.txt"])
        mock_client.get_container.assert_called_once()

    @patch("bots.storage.infomaniak_swift_utils.get_swift_client")
    def test_object_exists(self, mock_get_client):
        """Test object existence check utility"""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Test file exists
        result = object_exists("existing_file.txt")
        self.assertTrue(result)
        mock_client.head_object.assert_called_once()

        # Test file doesn't exist
        from swiftclient.exceptions import ClientException

        mock_client.head_object.side_effect = ClientException("Not Found", http_status=404)
        result = object_exists("non_existing_file.txt")
        self.assertFalse(result)


class StorageIntegrationTest(TransactionTestCase):
    """Test storage integration with Django models"""

    def setUp(self):
        """Set up test data"""
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(organization=self.organization, name="Test Project")
        self.bot = Bot.objects.create(project=self.project, name="Test Bot", meeting_url="https://test.com/meeting", state=BotStates.ENDED)

    @patch("django.db.models.fields.files.FieldFile.delete", autospec=True)
    @patch("django.db.models.fields.files.FieldFile.save", autospec=True)
    def test_recording_file_field_integration(self, mock_save, mock_delete):
        """Test that Recording model file field works with our storage"""

        def mock_file_save(instance, name, content, save=True):
            instance.name = name
            if save:
                instance.instance.save()

        def mock_file_delete(instance, save=True):
            instance.name = None
            if save:
                instance.instance.save()

        mock_save.side_effect = mock_file_save
        mock_delete.side_effect = mock_file_delete

        # Create recording
        recording = Recording.objects.create(bot=self.bot, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE)

        # Test file save
        test_content = ContentFile(b"test recording content")
        recording.file.save("test_recording.mp4", test_content)

        # Verify file was saved
        self.assertTrue(mock_save.called)
        self.assertIsNotNone(recording.file.name)

        # Test file delete
        recording.file.delete()

        # Verify file was deleted
        self.assertTrue(mock_delete.called)

    def test_storage_imports_work(self):
        """Test that all storage imports work correctly"""
        # Test that we can import all storage components
        from bots.storage import InfomaniakSwiftStorage
        from bots.storage.infomaniak_storage import InfomaniakSwiftStorage as DirectImport
        from bots.storage.infomaniak_swift_utils import (
            delete_file_from_swift,
            download_file_from_swift,
            generate_presigned_url,
            get_container_name,
            get_swift_client,
            list_objects_in_container,
            object_exists,
            upload_file_to_swift,
        )

        # Verify imports are the same
        self.assertEqual(InfomaniakSwiftStorage, DirectImport)

        # Verify functions are callable
        self.assertTrue(callable(upload_file_to_swift))
        self.assertTrue(callable(download_file_from_swift))
        self.assertTrue(callable(delete_file_from_swift))
        self.assertTrue(callable(list_objects_in_container))
        self.assertTrue(callable(object_exists))
        self.assertTrue(callable(get_swift_client))
        self.assertTrue(callable(get_container_name))
        self.assertTrue(callable(generate_presigned_url))
