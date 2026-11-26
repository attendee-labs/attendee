import json
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.models import ApiKey, Bot, BotChatMessageRequest, BotChatMessageRequestStates, BotChatMessageToOptions, BotStates, Project
from bots.zoom_web_bot_adapter.zoom_web_bot_adapter import ZoomWebBotAdapter


class ChatMessageThreadIdTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org", centicredits=10000)
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.api_key, self.api_key_plain = ApiKey.create(project=self.project, name="Test API Key")
        self.bot = Bot.objects.create(project=self.project, name="Test Bot", meeting_url="https://zoom.us/j/123456789", state=BotStates.JOINED_RECORDING)
        self.client = Client()

    def _send_chat_message(self, data):
        with patch("bots.bots_api_views.send_sync_command") as mock_send_sync_command:
            response = self.client.post(
                f"/api/v1/bots/{self.bot.object_id}/send_chat_message",
                data=json.dumps(data),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
            )
        self.assertEqual(response.status_code, 200, response.content)
        mock_send_sync_command.assert_called_once_with(self.bot, "sync_chat_message_requests")
        return BotChatMessageRequest.objects.get(bot=self.bot)

    def test_send_chat_message_stores_thread_id(self):
        chat_message_request = self._send_chat_message({"message": "Reply in thread", "thread_id": "1-{2857095b-fb9d-4bb1-b327-577ca242b1c2}"})

        self.assertEqual(chat_message_request.additional_data, {"thread_id": "1-{2857095b-fb9d-4bb1-b327-577ca242b1c2}"})
        self.assertEqual(chat_message_request.thread_id, "1-{2857095b-fb9d-4bb1-b327-577ca242b1c2}")

    def test_send_chat_message_without_thread_id(self):
        for data in [{"message": "No thread"}, {"message": "Null thread", "thread_id": None}]:
            with self.subTest(data=data):
                chat_message_request = self._send_chat_message(data)

                self.assertEqual(chat_message_request.additional_data, {})
                self.assertIsNone(chat_message_request.thread_id)
                chat_message_request.delete()

    def test_bot_controller_passes_thread_id_to_adapter(self):
        threaded_request = BotChatMessageRequest.objects.create(bot=self.bot, message="Threaded", to=BotChatMessageToOptions.EVERYONE, additional_data={"thread_id": "parent-msg-id"})
        unthreaded_request = BotChatMessageRequest.objects.create(bot=self.bot, message="Unthreaded", to=BotChatMessageToOptions.EVERYONE)

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()
        controller.adapter.is_ready_to_send_chat_messages.return_value = True

        controller.take_action_based_on_chat_message_requests_in_db()

        self.assertEqual(
            [call.kwargs for call in controller.adapter.send_chat_message.call_args_list],
            [
                {"text": "Threaded", "to_user_uuid": None, "thread_id": "parent-msg-id"},
                {"text": "Unthreaded", "to_user_uuid": None, "thread_id": None},
            ],
        )
        for chat_message_request in [threaded_request, unthreaded_request]:
            chat_message_request.refresh_from_db()
            self.assertEqual(chat_message_request.state, BotChatMessageRequestStates.SENT)

    def test_zoom_web_adapter_passes_thread_id_to_page(self):
        # Skip __init__, which sets up the browser; send_chat_message only needs the driver
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.driver = MagicMock()

        adapter.send_chat_message(text="Hello", to_user_uuid=None, thread_id="parent-msg-id")

        adapter.driver.execute_script.assert_called_once_with("window?.sendChatMessage(arguments[0], arguments[1], arguments[2]);", "Hello", None, "parent-msg-id")
