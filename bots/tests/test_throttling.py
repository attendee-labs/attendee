# bots/tests/test_throttling.py
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory

# Import the views module under test
from bots import bots_api_views as bots_views
from bots.throttling import ProjectPostThrottle


class ProjectThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()

        # Minimal request bodies for the endpoints we hit
        self.create_bot_payload = {"meeting_url": "https://zoom.us/j/123"}
        self.chat_msg_payload = {"text": "hello world", "text_to_speech_settings": {}}

        # Common view callables
        self.create_bot_view = bots_views.BotListCreateView.as_view()
        self.send_chat_view = bots_views.SendChatMessageView.as_view()

    def _authed_project(self, project_id: str):
        """
        Returns a (user, auth) tuple where auth has `.project.object_id = project_id`.
        """
        project = SimpleNamespace(object_id=project_id, id=None)
        auth = SimpleNamespace(project=project)
        return (None, auth)

    def _post_create_bot(self, mock_auth, project_id="projA"):
        mock_auth.return_value = self._authed_project(project_id)
        req = self.factory.post(
            "/bots/",
            self.create_bot_payload,
            format="json",
            HTTP_AUTHORIZATION="Token test",
            HTTP_CONTENT_TYPE="application/json",
        )
        return self.create_bot_view(req)

    def _post_send_chat(self, mock_auth, project_id="projA"):
        mock_auth.return_value = self._authed_project(project_id)
        req = self.factory.post(
            "/bots/bot_123/chat/",
            self.chat_msg_payload,
            format="json",
            HTTP_AUTHORIZATION="Token test",
            HTTP_CONTENT_TYPE="application/json",
        )
        return self.send_chat_view(req, object_id="bot_123")

    def test_throttle_blocks_third_post_in_window_for_same_project(self):
        """
        With rate 2/min, the 3rd POST for the same project should be 429.
        """
        with patch.object(bots_views.ApiKeyAuthentication, "authenticate") as mock_auth, patch.object(bots_views, "create_bot") as mock_create_bot, patch.object(bots_views, "launch_bot") as mock_launch_bot, patch.object(bots_views, "BotSerializer") as MockSerializer, patch.object(ProjectPostThrottle, "get_rate", return_value="2/min"):
            # Stub out internals of BotListCreateView
            dummy_bot = SimpleNamespace(object_id="bot_stub", state=bots_views.BotStates.JOINING)
            mock_create_bot.return_value = (dummy_bot, None)
            mock_launch_bot.return_value = None

            class _Ser:
                def __init__(self, obj):
                    self.data = {"id": getattr(obj, "object_id", "bot")}

            MockSerializer.side_effect = _Ser

            # Two allowed requests
            r1 = self._post_create_bot(mock_auth, "projA")
            r2 = self._post_create_bot(mock_auth, "projA")

            self.assertEqual(r1.status_code, 201)
            self.assertEqual(r2.status_code, 201)

            # Third should be throttled
            r3 = self._post_create_bot(mock_auth, "projA")
            self.assertEqual(r3.status_code, 429)

    def test_throttle_is_per_project_isolated_between_projects(self):
        """
        Hitting the limit on projA does not affect projB.
        """
        with patch.object(bots_views.ApiKeyAuthentication, "authenticate") as mock_auth, patch.object(bots_views, "create_bot") as mock_create_bot, patch.object(bots_views, "launch_bot") as mock_launch_bot, patch.object(bots_views, "BotSerializer") as MockSerializer, patch.object(ProjectPostThrottle, "get_rate", return_value="2/min"):
            dummy_bot = SimpleNamespace(object_id="bot_stub", state=bots_views.BotStates.JOINING)
            mock_create_bot.return_value = (dummy_bot, None)
            mock_launch_bot.return_value = None

            class _Ser:
                def __init__(self, obj):
                    self.data = {"id": getattr(obj, "object_id", "bot")}

            MockSerializer.side_effect = _Ser

            # Consume projA's allowance (2/min)
            r1 = self._post_create_bot(mock_auth, "projA")
            r2 = self._post_create_bot(mock_auth, "projA")
            self.assertEqual(r1.status_code, 201)
            self.assertEqual(r2.status_code, 201)

            # A different project should still be allowed
            r_other = self._post_create_bot(mock_auth, "projB")
            self.assertEqual(r_other.status_code, 201)

    def test_throttle_scope_is_shared_across_views(self):
        """
        Using the same scope ('project_post') across different POST endpoints should share the budget.
        After 2 POSTs to BotListCreateView, a POST to SendChatMessageView should be throttled (429).
        """
        with patch.object(bots_views.ApiKeyAuthentication, "authenticate") as mock_auth, patch.object(bots_views, "create_bot") as mock_create_bot, patch.object(bots_views, "launch_bot") as mock_launch_bot, patch.object(bots_views, "BotSerializer") as MockSerializer, patch.object(bots_views, "Bot") as MockBotModel, patch.object(bots_views, "BotChatMessageRequestSerializer") as MockChatSer, patch.object(bots_views, "create_bot_chat_message_request") as mock_create_msg_req, patch.object(bots_views, "send_sync_command") as mock_sync_cmd, patch.object(bots_views.BotEventManager, "is_state_that_can_play_media", return_value=True), patch.object(ProjectPostThrottle, "get_rate", return_value="2/min"):
            # ---- Set up CreateBot stubs
            dummy_bot = SimpleNamespace(object_id="bot_stub", state=bots_views.BotStates.JOINING)
            mock_create_bot.return_value = (dummy_bot, None)
            mock_launch_bot.return_value = None

            class _Ser:
                def __init__(self, obj):
                    self.data = {"id": getattr(obj, "object_id", "bot")}

            MockSerializer.side_effect = _Ser

            # ---- Set up SendChatMessageView stubs
            # Bot lookup
            MockBotModel.objects.get.return_value = SimpleNamespace(
                object_id="bot_123",
                state=bots_views.BotStates.JOINED_RECORDING,
            )

            class _ChatSer:
                def __init__(self, data):
                    self._data = data

                def is_valid(self):
                    return True

                @property
                def validated_data(self):
                    return self._data

            MockChatSer.side_effect = _ChatSer
            mock_create_msg_req.return_value = None
            mock_sync_cmd.return_value = None

            # Consume the two allowed POSTs on BotListCreateView
            r1 = self._post_create_bot(mock_auth, "projScope")
            r2 = self._post_create_bot(mock_auth, "projScope")
            self.assertEqual(r1.status_code, 201)
            self.assertEqual(r2.status_code, 201)

            # Now a POST to a *different* endpoint with same scope should be throttled
            r3 = self._post_send_chat(mock_auth, "projScope")
            self.assertEqual(r3.status_code, 429)
