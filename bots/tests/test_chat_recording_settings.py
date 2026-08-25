import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from accounts.models import Organization
from bots.bot_controller import BotController
from bots.bots_api_utils import BotCreationSource, create_bot
from bots.models import BotChatMessageRequest, BotChatMessageRequestStates, BotChatMessageToOptions, ChatMessage, Project
from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter
from bots.zoom_bot_adapter.zoom_bot_adapter import ZoomBotAdapter


class TestChatRecordingSettings(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def create_google_meet_bot(self, recording_settings=None):
        data = {
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "bot_name": "Test Bot",
        }
        if recording_settings is not None:
            data["recording_settings"] = recording_settings

        bot, error = create_bot(data=data, source=BotCreationSource.API, project=self.project)
        self.assertIsNone(error)
        return bot

    def test_chat_recording_defaults_to_enabled(self):
        bot = self.create_google_meet_bot()

        self.assertTrue(bot.record_chat_messages())
        self.assertTrue(bot.settings["recording_settings"]["record_chat_messages"])
        self.assertIsNotNone(BotController(bot.id).get_upsert_chat_message_callback())

    def test_disabling_chat_recording_prevents_incoming_messages_from_being_scheduled_or_saved(self):
        bot = self.create_google_meet_bot(
            {
                "record_chat_messages": False,
                "record_chat_messages_when_paused": True,
            }
        )
        controller = BotController(bot.id)
        controller.adapter = MagicMock()
        chat_message = {"text": "sensitive meeting chat"}

        self.assertFalse(bot.record_chat_messages())
        self.assertFalse(bot.record_chat_messages_when_paused())
        self.assertIsNone(controller.get_upsert_chat_message_callback())

        with patch("bots.bot_controller.bot_controller.GLib.idle_add") as idle_add:
            controller.on_new_chat_message(chat_message)
        idle_add.assert_not_called()

        controller.upsert_chat_message(chat_message)
        controller.adapter.get_participant.assert_not_called()
        self.assertFalse(ChatMessage.objects.filter(bot=bot).exists())

    def test_disabling_chat_recording_does_not_disable_outgoing_messages(self):
        bot = self.create_google_meet_bot({"record_chat_messages": False})
        request = BotChatMessageRequest.objects.create(
            bot=bot,
            message="A message from the bot",
            to=BotChatMessageToOptions.EVERYONE,
        )
        controller = BotController(bot.id)
        controller.adapter = MagicMock()
        controller.adapter.is_ready_to_send_chat_messages.return_value = True

        controller.take_action_based_on_chat_message_requests_in_db()

        controller.adapter.send_chat_message.assert_called_once_with(
            text="A message from the bot",
            to_user_uuid=None,
        )
        request.refresh_from_db()
        self.assertEqual(request.state, BotChatMessageRequestStates.SENT)


class TestDisabledChatRecordingAdapters(SimpleTestCase):
    def test_web_adapter_drops_chat_messages_before_logging_or_dispatch(self):
        adapter = object.__new__(WebBotAdapter)
        adapter.upsert_chat_message_callback = None
        message = (1).to_bytes(4, byteorder="little") + json.dumps(
            {
                "type": "ChatMessage",
                "text": "sensitive meeting chat",
            }
        ).encode("utf-8")

        with patch("bots.web_bot_adapter.web_bot_adapter.logger.info") as logger_info:
            adapter.handle_websocket([message])

        logger_info.assert_not_called()

    def test_zoom_adapter_does_not_read_chat_content_without_callback(self):
        adapter = object.__new__(ZoomBotAdapter)
        adapter.upsert_chat_message_callback = None
        chat_message = MagicMock()

        adapter.on_chat_msg_notification_callback(chat_message, None)

        chat_message.GetContent.assert_not_called()
