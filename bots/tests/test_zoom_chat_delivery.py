from unittest.mock import MagicMock, patch

import zoom_meeting_sdk as zoom
from django.test import TestCase, tag

from bots.bot_adapter import BotAdapter, ChatMessageSendError
from bots.bot_controller import BotController
from bots.bot_controller.chat_message_sender import ChatMessageSender
from bots.models import Bot, BotChatMessageRequest, BotChatMessageRequestStates, BotChatMessageToOptions, BotStates, Organization, Project
from bots.zoom_bot_adapter.zoom_bot_adapter import ZoomBotAdapter


@tag("zoom_tests")
class TestZoomChatDelivery(TestCase):
    def setUp(self):
        project = Project.objects.create(name="Chat delivery", organization=Organization.objects.create(name="Chat delivery"))
        self.bot = Bot.objects.create(project=project, name="Bot", meeting_url="https://zoom.us/j/123456789", state=BotStates.JOINED_NOT_RECORDING)
        self.adapter = ZoomBotAdapter.__new__(ZoomBotAdapter)
        self.adapter.ready_to_send_chat_messages = True
        self.adapter.chat_ctrl = MagicMock()
        self.sdk_send = self.adapter.chat_ctrl.SendChatMsgTo
        self.sdk_send.return_value = zoom.SDKError.SDKERR_SUCCESS
        self.sender = ChatMessageSender(self.bot)
        clock_patch = patch("bots.bot_controller.chat_message_sender.time.monotonic", return_value=100.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def request(self, recipient="101"):
        return BotChatMessageRequest.objects.create(bot=self.bot, message="Hello from the bot", to=BotChatMessageToOptions.SPECIFIC_USER, to_user_uuid=recipient)

    def assert_state(self, request, state):
        request.refresh_from_db()
        self.assertEqual(request.state, state)
        if state == BotChatMessageRequestStates.SENT:
            self.assertIsNotNone(request.sent_at_timestamp_ms)
            self.assertIsNone(request.failure_data)
        else:
            self.assertIsNone(request.sent_at_timestamp_ms)

    def test_dm_burst_is_paced_and_rejected_dm_retries_without_new_webhook(self):
        first = self.request()
        second = self.request("202")
        self.sdk_send.side_effect = [zoom.SDKError.SDKERR_SUCCESS, zoom.SDKError.SDKERR_TOO_FREQUENT_CALL, zoom.SDKError.SDKERR_SUCCESS]

        self.sender.send_pending(self.adapter)
        self.assert_state(first, BotChatMessageRequestStates.SENT)
        self.assert_state(second, BotChatMessageRequestStates.ENQUEUED)
        self.clock.return_value = 100.1
        self.sender.send_pending(self.adapter)
        self.assertEqual(self.sdk_send.call_count, 1)

        self.clock.return_value = 102
        self.sender.process(self.adapter)
        self.assert_state(second, BotChatMessageRequestStates.ENQUEUED)
        self.clock.return_value = 103.999
        self.sender.send_pending(self.adapter)
        self.assertEqual(self.sdk_send.call_count, 2)
        self.clock.return_value = 104
        self.sender.process(self.adapter)
        self.assert_state(second, BotChatMessageRequestStates.SENT)
        self.sender.send_pending(self.adapter)
        self.assertEqual(self.sdk_send.call_count, 3)
        builder = self.adapter.chat_ctrl.GetChatMessageBuilder.return_value
        self.assertEqual([call.args[0] for call in builder.SetReceiver.call_args_list], ["101", "202", "202"])
        self.assertEqual(builder.Clear.call_count, 3)

    def test_controller_main_loop_keeps_processing_audio_during_retry(self):
        request = self.request()
        controller = BotController(self.bot.id)
        controller.adapter = self.adapter
        controller.first_timeout_call = False
        controller.screen_and_audio_recorder = None
        controller.audio_chunk_uploader = None
        for name in ("per_participant_non_streaming_audio_input_manager", "per_participant_streaming_audio_input_manager", "closed_caption_manager", "audio_output_manager", "video_output_manager", "bot_resource_snapshot_taker"):
            setattr(controller, name, MagicMock())
        self.adapter.check_auto_leave_conditions = MagicMock()
        self.sdk_send.side_effect = [zoom.SDKError.SDKERR_TOO_FREQUENT_CALL, zoom.SDKError.SDKERR_SUCCESS]
        controller.take_action_based_on_chat_message_requests_in_db()
        self.assert_state(request, BotChatMessageRequestStates.ENQUEUED)
        with patch("time.sleep", side_effect=AssertionError("Chat delivery must not block the main loop")):
            self.clock.return_value = 101
            self.assertTrue(controller.on_main_loop_timeout())
            self.assertEqual(self.sdk_send.call_count, 1)
            self.clock.return_value = 102
            self.assertTrue(controller.on_main_loop_timeout())
        self.assert_state(request, BotChatMessageRequestStates.SENT)
        self.assertEqual(controller.per_participant_non_streaming_audio_input_manager.process_chunks.call_count, 2)

    def test_cooldown_applies_to_new_requests_after_queue_drains(self):
        self.request()
        self.sender.send_pending(self.adapter)
        self.assertFalse(self.sender.pending)
        second = self.request("202")
        self.clock.return_value = 100.1
        self.sender.send_pending(self.adapter)
        self.assert_state(second, BotChatMessageRequestStates.ENQUEUED)
        self.assertEqual(self.sdk_send.call_count, 1)
        self.clock.return_value = 102
        self.sender.process(self.adapter)
        self.assert_state(second, BotChatMessageRequestStates.SENT)

    def test_retries_back_off_and_exhaustion_does_not_block_next_recipient(self):
        failed = self.request()
        next_request = self.request("202")
        self.sdk_send.return_value = zoom.SDKError.SDKERR_TOO_FREQUENT_CALL
        self.sender.send_pending(self.adapter)
        for timestamp, attempts in [(102, 2), (106, 3), (114, 4), (130, 5)]:
            self.clock.return_value = timestamp - 0.001
            self.sender.process(self.adapter)
            self.assertEqual(self.sdk_send.call_count, attempts - 1)
            self.clock.return_value = timestamp
            self.sender.process(self.adapter)
            self.assertEqual(self.sdk_send.call_count, attempts)
        self.assert_state(failed, BotChatMessageRequestStates.FAILED)
        self.assertEqual(failed.failure_data, {"reason": "retry_exhausted", "error_code": str(zoom.SDKError.SDKERR_TOO_FREQUENT_CALL), "attempts": 5})
        self.sdk_send.return_value = zoom.SDKError.SDKERR_SUCCESS
        self.clock.return_value = 132
        self.sender.process(self.adapter)
        self.assert_state(next_request, BotChatMessageRequestStates.SENT)

    def test_retry_deadline_prevents_send_after_main_loop_delay(self):
        request = self.request()
        self.sdk_send.return_value = zoom.SDKError.SDKERR_TOO_FREQUENT_CALL
        self.sender.send_pending(self.adapter)
        self.clock.return_value = 160
        self.sender.process(self.adapter)
        self.assert_state(request, BotChatMessageRequestStates.FAILED)
        self.assertEqual(request.failure_data["reason"], "retry_deadline_exceeded")
        self.assertEqual(self.sdk_send.call_count, 1)

    def test_permanent_sdk_errors_are_failed_without_retry(self):
        for error in (zoom.SDKError.SDKERR_NO_PERMISSION, zoom.SDKError.SDKERR_INVALID_PARAMETER, zoom.SDKError.SDKERR_NOT_IN_MEETING):
            with self.subTest(error=error):
                request = self.request()
                self.sdk_send.return_value = error
                self.sender.send_pending(self.adapter)
                self.assert_state(request, BotChatMessageRequestStates.FAILED)
                self.assertEqual(request.failure_data, {"reason": "send_rejected", "error_code": str(error), "attempts": 1})
                self.clock.return_value += 2
                calls = self.sdk_send.call_count
                self.sender.process(self.adapter)
                self.assertEqual(self.sdk_send.call_count, calls)

    def test_stopping_bot_cancels_retry_and_unsent_recipients(self):
        retry = self.request()
        unsent = self.request("202")
        self.sdk_send.return_value = zoom.SDKError.SDKERR_TOO_FREQUENT_CALL
        self.sender.send_pending(self.adapter)
        self.sender.stop()
        self.clock.return_value = 102
        self.sender.process(self.adapter)
        self.sender.send_pending(self.adapter)
        self.assertEqual(self.sdk_send.call_count, 1)
        for request in (retry, unsent):
            self.assert_state(request, BotChatMessageRequestStates.FAILED)
            self.assertEqual(request.failure_data["reason"], "bot_stopped")

    def test_no_retry_while_leaving_or_adapter_not_ready(self):
        request = self.request()
        self.sdk_send.return_value = zoom.SDKError.SDKERR_TOO_FREQUENT_CALL
        self.sender.send_pending(self.adapter)
        self.clock.return_value = 102
        self.bot.state = BotStates.LEAVING
        self.sender.process(self.adapter)
        self.bot.state = BotStates.JOINED_NOT_RECORDING
        self.adapter.ready_to_send_chat_messages = False
        self.sender.process(self.adapter)
        self.assertEqual(self.sdk_send.call_count, 1)
        self.assert_state(request, BotChatMessageRequestStates.ENQUEUED)
        self.adapter.ready_to_send_chat_messages = True
        self.sdk_send.return_value = zoom.SDKError.SDKERR_SUCCESS
        self.sender.process(self.adapter)
        self.assert_state(request, BotChatMessageRequestStates.SENT)

    def test_welcome_message_waits_for_join_and_readiness(self):
        request = self.request()
        self.bot.state = BotStates.JOINING
        self.sender.send_pending(self.adapter)
        self.sdk_send.assert_not_called()
        self.bot.state = BotStates.JOINED_NOT_RECORDING
        self.sender.process(self.adapter)
        self.assert_state(request, BotChatMessageRequestStates.SENT)

    def test_other_adapters_keep_existing_immediate_delivery(self):
        first, second = self.request(), self.request("202")
        adapter = MagicMock()
        adapter.CHAT_MESSAGE_INTERVAL_SECONDS = BotAdapter.CHAT_MESSAGE_INTERVAL_SECONDS
        self.sender.send_pending(adapter)
        self.assertEqual(adapter.send_chat_message.call_count, 2)
        self.assert_state(first, BotChatMessageRequestStates.SENT)
        self.assert_state(second, BotChatMessageRequestStates.SENT)

    def test_sdk_message_builder_is_cleared_even_if_send_raises(self):
        self.sdk_send.side_effect = RuntimeError("SDK failure")
        with self.assertRaises(RuntimeError):
            self.adapter.send_chat_message("Hello from the bot", "101")
        self.adapter.chat_ctrl.GetChatMessageBuilder.return_value.Clear.assert_called_once()

    def test_missing_builder_or_message_never_marks_request_sent(self):
        builder = self.adapter.chat_ctrl.GetChatMessageBuilder.return_value
        for result in (None, builder):
            with self.subTest(builder=result):
                self.adapter.chat_ctrl.GetChatMessageBuilder.return_value = result
                builder.Build.return_value = None
                with self.assertRaises(ChatMessageSendError):
                    self.adapter.send_chat_message("Hello from the bot", "101")
                self.sdk_send.assert_not_called()
