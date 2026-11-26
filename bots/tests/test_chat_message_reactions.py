import json
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.models import ApiKey, Bot, BotChatMessageReactionRequest, BotChatMessageRequestStates, BotStates, ChatMessage, ChatMessageToOptions, Participant, Project
from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter
from bots.zoom_web_bot_adapter.zoom_web_bot_adapter import ZoomWebBotAdapter


class ChatMessageReactionTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org", centicredits=10000)
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.api_key, self.api_key_plain = ApiKey.create(project=self.project, name="Test API Key")
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://zoom.us/j/123456789",
            state=BotStates.JOINED_RECORDING,
            settings={"zoom_settings": {"sdk": "web"}},
        )
        participant = Participant.objects.create(bot=self.bot, uuid="16778240", full_name="Alice")
        self.chat_message = ChatMessage.objects.create(
            bot=self.bot,
            participant=participant,
            text="Can you take a look?",
            to=ChatMessageToOptions.EVERYONE,
            timestamp=1764000000,
            source_uuid="rec_abcdefghijklmnop-1-{2857095b-fb9d-4bb1-b327-577ca242b1c2}",
        )
        self.client = Client()

    def _post_reaction(self, data, bot=None):
        bot = bot or self.bot
        with patch("bots.bots_api_views.send_sync_command") as mock_send_sync_command:
            response = self.client.post(
                f"/api/v1/bots/{bot.object_id}/send_chat_message_reaction",
                data=json.dumps(data),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
            )
        return response, mock_send_sync_command

    def test_add_reaction_creates_request(self):
        response, mock_send_sync_command = self._post_reaction({"message_id": self.chat_message.object_id, "emoji": "👀"})

        self.assertEqual(response.status_code, 200, response.content)
        mock_send_sync_command.assert_called_once_with(self.bot, "sync_chat_message_reaction_requests")
        reaction_request = BotChatMessageReactionRequest.objects.get(bot=self.bot)
        self.assertEqual(reaction_request.chat_message, self.chat_message)
        self.assertEqual(reaction_request.emoji, "👀")
        self.assertEqual(reaction_request.action, "add")
        self.assertEqual(reaction_request.state, BotChatMessageRequestStates.ENQUEUED)

    def test_remove_reaction_creates_request(self):
        response, _ = self._post_reaction({"message_id": self.chat_message.object_id, "emoji": "👀", "action": "remove"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(BotChatMessageReactionRequest.objects.get(bot=self.bot).action, "remove")

    def test_invalid_action_is_rejected(self):
        response, _ = self._post_reaction({"message_id": self.chat_message.object_id, "emoji": "👀", "action": "toggle"})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("action", response.json())

    def test_unknown_chat_message_returns_404(self):
        response, mock_send_sync_command = self._post_reaction({"message_id": "msg_doesnotexist0000", "emoji": "👀"})

        self.assertEqual(response.status_code, 404, response.content)
        mock_send_sync_command.assert_not_called()

    def test_chat_message_from_another_bot_returns_404(self):
        other_bot = Bot.objects.create(project=self.project, name="Other Bot", meeting_url="https://zoom.us/j/987654321", state=BotStates.JOINED_RECORDING, settings={"zoom_settings": {"sdk": "web"}})

        response, _ = self._post_reaction({"message_id": self.chat_message.object_id, "emoji": "👀"}, bot=other_bot)

        self.assertEqual(response.status_code, 404, response.content)

    def test_unsupported_bots_are_rejected(self):
        native_zoom_bot = Bot.objects.create(project=self.project, name="Native Zoom Bot", meeting_url="https://zoom.us/j/111111111", state=BotStates.JOINED_RECORDING)
        google_meet_bot = Bot.objects.create(project=self.project, name="Meet Bot", meeting_url="https://meet.google.com/abc-defg-hij", state=BotStates.JOINED_RECORDING)
        teams_bot = Bot.objects.create(project=self.project, name="Teams Bot", meeting_url="https://teams.microsoft.com/l/meetup-join/abc", state=BotStates.JOINED_RECORDING)

        for bot in [native_zoom_bot, google_meet_bot, teams_bot]:
            with self.subTest(bot=bot.name):
                response, mock_send_sync_command = self._post_reaction({"message_id": self.chat_message.object_id, "emoji": "👀"}, bot=bot)

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json(), {"error": "Chat message reactions are only supported for Zoom bots that use the web SDK"})
                mock_send_sync_command.assert_not_called()

    def test_bot_not_in_meeting_is_rejected(self):
        self.bot.state = BotStates.ENDED
        self.bot.save()

        response, mock_send_sync_command = self._post_reaction({"message_id": self.chat_message.object_id, "emoji": "👀"})

        self.assertEqual(response.status_code, 400, response.content)
        mock_send_sync_command.assert_not_called()

    def test_platform_message_uuid(self):
        self.assertEqual(self.chat_message.platform_message_uuid, "1-{2857095b-fb9d-4bb1-b327-577ca242b1c2}")
        self.assertIsNone(ChatMessage(source_uuid=None).platform_message_uuid)

    def test_bot_controller_passes_reaction_to_adapter(self):
        add_request = BotChatMessageReactionRequest.objects.create(bot=self.bot, chat_message=self.chat_message, emoji="👀", action="add")
        remove_request = BotChatMessageReactionRequest.objects.create(bot=self.bot, chat_message=self.chat_message, emoji="👀", action="remove")

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()
        controller.adapter.is_ready_to_send_chat_messages.return_value = True

        controller.take_action_based_on_chat_message_reaction_requests_in_db()

        self.assertEqual(
            [call.kwargs for call in controller.adapter.send_chat_message_reaction.call_args_list],
            [
                {"message_uuid": "1-{2857095b-fb9d-4bb1-b327-577ca242b1c2}", "emoji": "👀", "action": "add"},
                {"message_uuid": "1-{2857095b-fb9d-4bb1-b327-577ca242b1c2}", "emoji": "👀", "action": "remove"},
            ],
        )
        for reaction_request in [add_request, remove_request]:
            reaction_request.refresh_from_db()
            self.assertEqual(reaction_request.state, BotChatMessageRequestStates.SENT)
            self.assertIsNotNone(reaction_request.sent_at_timestamp_ms)

    def test_bot_controller_waits_until_adapter_can_send_chat_messages(self):
        reaction_request = BotChatMessageReactionRequest.objects.create(bot=self.bot, chat_message=self.chat_message, emoji="✅")

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()
        controller.adapter.is_ready_to_send_chat_messages.return_value = False

        controller.take_action_based_on_chat_message_reaction_requests_in_db()

        controller.adapter.send_chat_message_reaction.assert_not_called()
        reaction_request.refresh_from_db()
        self.assertEqual(reaction_request.state, BotChatMessageRequestStates.ENQUEUED)

    def test_zoom_web_adapter_passes_reaction_to_page(self):
        # Skip __init__, which sets up the browser; send_chat_message_reaction only needs the driver
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.driver = MagicMock()

        adapter.send_chat_message_reaction(message_uuid="parent-msg-id", emoji="👀", action="add")

        adapter.driver.execute_script.assert_called_once_with("window?.sendChatMessageReaction(arguments[0], arguments[1], arguments[2]);", "parent-msg-id", "👀", "add")

    def test_other_web_adapters_do_not_touch_the_page(self):
        adapter = WebBotAdapter.__new__(WebBotAdapter)
        adapter.driver = MagicMock()

        adapter.send_chat_message_reaction(message_uuid="parent-msg-id", emoji="👀", action="add")

        adapter.driver.execute_script.assert_not_called()
