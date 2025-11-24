import json
from unittest.mock import MagicMock, Mock, patch

from django.test import TestCase
from kubernetes import client

from bots.bot_pod_creator.bot_pod_creator import BotPodCreator, apply_json6902_patch
from bots.bot_pod_creator.bot_pod_spec import BotPodSpecType


class TestApplyJson6902Patch(TestCase):
    """Test the apply_json6902_patch helper function"""

    def test_empty_patch_returns_original(self):
        """Test that an empty patch returns the original JSON"""
        original = {"name": "test", "value": 123}
        result = apply_json6902_patch(original, "")
        self.assertEqual(result, original)

    def test_valid_add_operation(self):
        """Test adding a field with JSON6902 patch"""
        original = {"name": "test"}
        patch = '[{"op": "add", "path": "/newField", "value": "newValue"}]'
        result = apply_json6902_patch(original, patch)
        self.assertEqual(result["newField"], "newValue")
        self.assertEqual(result["name"], "test")

    def test_valid_replace_operation(self):
        """Test replacing a field with JSON6902 patch"""
        original = {"name": "test", "value": 123}
        patch = '[{"op": "replace", "path": "/value", "value": 456}]'
        result = apply_json6902_patch(original, patch)
        self.assertEqual(result["value"], 456)

    def test_invalid_json_returns_original(self):
        """Test that invalid JSON patch returns original"""
        original = {"name": "test"}
        patch = "not valid json"
        result = apply_json6902_patch(original, patch)
        self.assertEqual(result, original)

    def test_non_array_patch_returns_original(self):
        """Test that non-array patch returns original"""
        original = {"name": "test"}
        patch = '{"op": "add", "path": "/field", "value": "val"}'
        result = apply_json6902_patch(original, patch)
        self.assertEqual(result, original)

    def test_invalid_patch_operation_returns_original(self):
        """Test that invalid patch operation returns original"""
        original = {"name": "test"}
        patch = '[{"op": "add", "path": "/invalid/nested/path/that/does/not/exist", "value": "val"}]'
        result = apply_json6902_patch(original, patch)
        self.assertEqual(result, original)


class TestBotPodCreator(TestCase):
    """Test the BotPodCreator class"""

    def setUp(self):
        """Set up test fixtures"""
        # Mock environment variables
        self.env_vars = {
            "CUBER_APP_NAME": "attendee",
            "CUBER_RELEASE_VERSION": "abc123-1234567890",
            "BOT_POD_NAMESPACE": "bot-namespace",
            "WEBPAGE_STREAMER_POD_NAMESPACE": "streamer-namespace",
            "BOT_POD_IMAGE": "test-registry/attendee",
            "BOT_POD_SPEC_DEFAULT": "",
            "BOT_POD_SPEC_SCHEDULED": "",
        }

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_initialization_loads_config(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test that BotPodCreator initializes with proper config"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"

        # Should try in-cluster config first, then fall back to kube config
        from kubernetes.config import ConfigException
        mock_config.ConfigException = ConfigException
        mock_config.load_incluster_config.side_effect = ConfigException()
        
        creator = BotPodCreator()

        mock_config.load_incluster_config.assert_called_once()
        mock_config.load_kube_config.assert_called_once()
        self.assertEqual(creator.namespace, "bot-namespace")
        self.assertEqual(creator.app_name, "attendee")
        self.assertEqual(creator.app_version, "abc123-1234567890")

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_fetch_bot_pod_spec_valid_type(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test fetching bot pod spec with valid type"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        creator = BotPodCreator()
        
        result = creator.fetch_bot_pod_spec("DEFAULT")
        self.assertEqual(result, "")

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_fetch_bot_pod_spec_invalid_type(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test that fetch_bot_pod_spec rejects invalid types"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        creator = BotPodCreator()
        
        # Test with lowercase
        with self.assertRaises(ValueError):
            creator.fetch_bot_pod_spec("default")
        
        # Test with numbers
        with self.assertRaises(ValueError):
            creator.fetch_bot_pod_spec("DEFAULT123")
        
        # Test with special characters
        with self.assertRaises(ValueError):
            creator.fetch_bot_pod_spec("DEFAULT_SPEC")

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_create_bot_pod_success(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test creating a bot pod successfully"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        # Mock the V1 API
        mock_v1 = MagicMock()
        mock_api_response = MagicMock()
        mock_api_response.metadata.name = "bot-123-abc"
        mock_api_response.status.phase = "Pending"
        mock_v1.create_namespaced_pod.return_value = mock_api_response
        mock_client.CoreV1Api.return_value = mock_v1
        
        # Mock ApiClient
        mock_api_client = MagicMock()
        mock_api_client.sanitize_for_serialization.return_value = {"kind": "Pod", "metadata": {"name": "bot-123-abc"}}
        mock_client.ApiClient.return_value = mock_api_client
        
        creator = BotPodCreator()
        result = creator.create_bot_pod(bot_id=123, bot_name="bot-123-abc")
        
        self.assertTrue(result["created"])
        self.assertEqual(result["name"], "bot-123-abc")
        self.assertEqual(result["status"], "Pending")
        self.assertIn("test-registry/attendee:abc123-1234567890", result["image"])
        mock_v1.create_namespaced_pod.assert_called_once()

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_create_bot_pod_with_webpage_streamer(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test creating a bot pod with webpage streamer"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        # Mock the V1 API
        mock_v1 = MagicMock()
        mock_bot_response = MagicMock()
        mock_bot_response.metadata.name = "bot-123-abc"
        mock_bot_response.status.phase = "Pending"
        
        mock_streamer_response = MagicMock()
        mock_streamer_response.metadata.name = "bot-123-abc-webpage-streamer"
        mock_streamer_response.metadata.uid = "streamer-uid-123"
        
        mock_v1.create_namespaced_pod.side_effect = [mock_bot_response, mock_streamer_response]
        mock_v1.create_namespaced_service.return_value = MagicMock()
        mock_client.CoreV1Api.return_value = mock_v1
        
        # Mock ApiClient
        mock_api_client = MagicMock()
        mock_api_client.sanitize_for_serialization.return_value = {"kind": "Pod"}
        mock_client.ApiClient.return_value = mock_api_client
        
        creator = BotPodCreator()
        result = creator.create_bot_pod(
            bot_id=123,
            bot_name="bot-123-abc",
            add_webpage_streamer=True
        )
        
        self.assertTrue(result["created"])
        # Should create both bot pod and streamer pod
        self.assertEqual(mock_v1.create_namespaced_pod.call_count, 2)
        # Should create service for streamer
        mock_v1.create_namespaced_service.assert_called_once()

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_create_bot_pod_api_exception(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test handling of API exception during pod creation"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        # Mock the V1 API to raise an exception
        mock_v1 = MagicMock()
        mock_v1.create_namespaced_pod.side_effect = client.ApiException("Error creating pod")
        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.ApiException = client.ApiException
        
        # Mock ApiClient
        mock_api_client = MagicMock()
        mock_api_client.sanitize_for_serialization.return_value = {"kind": "Pod"}
        mock_client.ApiClient.return_value = mock_api_client
        
        creator = BotPodCreator()
        result = creator.create_bot_pod(bot_id=123, bot_name="bot-123-abc")
        
        self.assertFalse(result["created"])
        self.assertEqual(result["status"], "Error")
        self.assertIn("error", result)

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_delete_bot_pod_success(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test deleting a bot pod successfully"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        # Mock the V1 API
        mock_v1 = MagicMock()
        mock_v1.delete_namespaced_pod.return_value = MagicMock()
        mock_client.CoreV1Api.return_value = mock_v1
        
        creator = BotPodCreator()
        result = creator.delete_bot_pod("bot-123-abc")
        
        self.assertTrue(result["deleted"])
        mock_v1.delete_namespaced_pod.assert_called_once_with(
            name="bot-123-abc",
            namespace="bot-namespace",
            grace_period_seconds=60
        )

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_delete_bot_pod_api_exception(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test handling of API exception during pod deletion"""
        mock_getenv.side_effect = lambda key, default=None: self.env_vars.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        # Mock the V1 API to raise an exception
        mock_v1 = MagicMock()
        mock_v1.delete_namespaced_pod.side_effect = client.ApiException("Pod not found")
        mock_client.CoreV1Api.return_value = mock_v1
        mock_client.ApiException = client.ApiException
        
        creator = BotPodCreator()
        result = creator.delete_bot_pod("bot-123-abc")
        
        self.assertFalse(result["deleted"])
        self.assertIn("error", result)

    @patch("bots.bot_pod_creator.bot_pod_creator.config")
    @patch("bots.bot_pod_creator.bot_pod_creator.client")
    @patch("bots.bot_pod_creator.bot_pod_creator.os.getenv")
    @patch("bots.bot_pod_creator.bot_pod_creator.settings")
    def test_apply_spec_to_bot_pod(self, mock_settings, mock_getenv, mock_client, mock_config):
        """Test applying a spec to bot pod"""
        env_vars_with_spec = self.env_vars.copy()
        env_vars_with_spec["BOT_POD_SPEC_DEFAULT"] = '[{"op": "add", "path": "/metadata/labels/custom", "value": "test"}]'
        mock_getenv.side_effect = lambda key, default=None: env_vars_with_spec.get(key, default)
        mock_settings.BOT_POD_NAMESPACE = "bot-namespace"
        mock_settings.WEBPAGE_STREAMER_POD_NAMESPACE = "streamer-namespace"
        
        # Mock ApiClient
        mock_api_client = MagicMock()
        mock_api_client.sanitize_for_serialization.return_value = {
            "metadata": {"name": "test", "labels": {}}
        }
        mock_client.ApiClient.return_value = mock_api_client
        
        creator = BotPodCreator()
        creator.bot_pod_spec = '[{"op": "add", "path": "/metadata/labels/custom", "value": "test"}]'
        
        mock_pod = MagicMock(spec=client.V1Pod)
        result = creator.apply_spec_to_bot_pod(mock_pod)
        
        self.assertEqual(result["metadata"]["labels"]["custom"], "test")

