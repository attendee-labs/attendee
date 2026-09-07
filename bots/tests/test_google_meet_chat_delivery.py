import json
from pathlib import Path
from unittest.mock import Mock

from django.test import TestCase, tag
from gi.repository import GLib
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

from bots.bot_controller import BotController
from bots.google_meet_bot_adapter import GoogleMeetBotAdapter
from bots.models import Bot, BotChatMessageRequest, BotChatMessageRequestStates, Organization, Project
from bots.teams_bot_adapter import TeamsBotAdapter
from bots.zoom_bot_adapter import ZoomBotAdapter
from bots.zoom_web_bot_adapter import ZoomWebBotAdapter

ROOT = Path(__file__).resolve().parents[1]


@tag("google_meet_tests")
class TestGoogleMeetChatDelivery(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        cls.driver = webdriver.Chrome(service=Service("/usr/local/bin/chromedriver"), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def setUp(self):
        self.driver.get((ROOT / "tests/fixtures/google_meet_chat.html").as_uri())
        self.driver.execute_script((ROOT / "google_meet_bot_adapter/google_meet_chat_sender.js").read_text())
        self.driver.execute_script("""
            GoogleMeetChatSender.ATTEMPT_TIMEOUT_MS = 300;
            GoogleMeetChatSender.RETRY_DELAY_MS = 10;
            GoogleMeetChatSender.POLL_INTERVAL_MS = 10;
            window.sender = new GoogleMeetChatSender({sendJson: result => results.push(result)}, {currentUserId: 'bot'});
            window.googleMeetChatSender = sender;
        """)
        organization = Organization.objects.create(name="Chat delivery test")
        project = Project.objects.create(name="Chat delivery test", organization=organization)
        self.bot = Bot.objects.create(project=project, meeting_url="https://meet.google.com/abc-defg-hij", name="Bot")
        self.controller = BotController(self.bot.id)
        # Use the real adapter's send and websocket paths, without joining a meeting.
        self.adapter = GoogleMeetBotAdapter.__new__(GoogleMeetBotAdapter)
        self.adapter.driver = self.driver
        self.adapter.ready_to_send_chat_messages = True
        self.adapter.send_message_callback = self.controller.on_message_from_adapter
        self.controller.adapter = self.adapter
        self.addCleanup(self.close_pending_request)

    def close_pending_request(self):
        self.controller.cleanup_called = True
        if self.controller.pending_chat_message_request:
            self.controller.finish_chat_message_request({"request_id": self.controller.pending_chat_message_request.id, "status": "failed", "error": "test_cleanup"})
        self.drain_callbacks()

    def drain_callbacks(self):
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def create_request(self, text="Example message: https://example.com/resource#test"):
        return BotChatMessageRequest.objects.create(bot=self.bot, to="everyone", message=text)

    def deliver_result(self):
        result = WebDriverWait(self.driver, 5).until(lambda driver: driver.execute_script("return results.shift() || null;"))
        self.adapter.handle_websocket([(1).to_bytes(4, "little") + json.dumps(result).encode()])
        self.drain_callbacks()
        return result

    def test_delivery_outcomes(self):
        cases = [
            (["confirmed"], "sent", None, 1, 0),
            (["rejected", "confirmed"], "sent", None, 2, 1),
            (["rejected", "rejected", "rejected"], "failed", "delivery_failed", 3, 2),
            (["unknown"], "failed", "delivery_unconfirmed", 1, 0),
            (["other_user"], "failed", "delivery_unconfirmed", 1, 0),
            (["old_echo"], "failed", "delivery_unconfirmed", 1, 0),
            (["ack_without_dom"], "failed", "delivery_unconfirmed", 1, 0),
            (["stuck"], "failed", "delivery_unconfirmed", 2, 1),
        ]
        for outcomes, status, error, attempts, resends in cases:
            with self.subTest(outcomes=outcomes):
                self.driver.execute_script("document.getElementById('history').replaceChildren(); results.length = 0; submissions = 0; resends = 0;")
                self.driver.execute_script("window.outcomes = arguments[0];", outcomes)
                request = self.create_request()
                self.controller.take_action_based_on_chat_message_requests_in_db()
                request.refresh_from_db()
                self.assertEqual(request.state, BotChatMessageRequestStates.ENQUEUED)
                self.assertIsNone(request.sent_at_timestamp_ms)
                # A repeated sync while the UI is waiting must not submit again.
                self.controller.take_action_based_on_chat_message_requests_in_db()
                result = self.deliver_result()
                self.assertEqual((result["status"], result["error"], result["attempts"]), (status, error, attempts))
                self.assertEqual(self.driver.execute_script("return [submissions, resends];"), [1, resends])
                request.refresh_from_db()
                if status == "sent":
                    self.assertEqual(request.state, BotChatMessageRequestStates.SENT)
                    self.assertIsNotNone(request.sent_at_timestamp_ms)
                else:
                    self.assertEqual(request.state, BotChatMessageRequestStates.FAILED)
                    self.assertIsNone(request.sent_at_timestamp_ms)
                    self.assertEqual(request.failure_data, {"error": error, "attempts": attempts})

    def test_old_identical_failed_message_is_not_retried(self):
        request = self.create_request()
        self.driver.execute_script(
            r"""
            const old = addMessage('old-message', arguments[0]);
            const error = document.createElement('div');
            error.innerHTML = '<span>Your message wasn\'t delivered</span><button aria-label="Resend">Resend</button>';
            error.querySelector('button').onclick = () => oldResends++;
            old.prepend(error);
            outcomes = ['rejected', 'confirmed'];
        """,
            request.message,
        )
        self.controller.take_action_based_on_chat_message_requests_in_db()
        self.assertEqual(self.deliver_result()["status"], "sent")
        self.assertEqual(self.driver.execute_script("return [oldResends, submissions, resends];"), [0, 1, 1])

    def test_missing_or_disabled_input_is_failed(self):
        for action in ["document.querySelector('textarea').disabled = true", "document.querySelector('textarea').remove()"]:
            with self.subTest(action=action):
                request = self.create_request()
                self.driver.execute_script(action)
                self.controller.take_action_based_on_chat_message_requests_in_db()
                result = self.deliver_result()
                self.assertEqual((result["status"], result["error"], result["attempts"]), ("failed", "chat_input_unavailable", 0))
                request.refresh_from_db()
                self.assertEqual(request.state, BotChatMessageRequestStates.FAILED)

    def test_queue_advances_and_duplicate_result_cannot_complete_next_request(self):
        self.driver.execute_script("outcomes = ['confirmed', 'confirmed'];")
        first = self.create_request()
        second = self.create_request()
        self.controller.take_action_based_on_chat_message_requests_in_db()
        first_result = self.deliver_result()
        self.assertEqual(first_result["request_id"], first.id)
        self.assertEqual(self.controller.pending_chat_message_request.id, second.id)
        self.controller.finish_chat_message_request(first_result)
        second.refresh_from_db()
        self.assertEqual(second.state, BotChatMessageRequestStates.ENQUEUED)
        self.assertEqual(self.deliver_result()["request_id"], second.id)
        self.assertEqual(self.driver.execute_script("return submissions;"), 2)
        self.assertEqual(list(self.bot.chat_message_requests.values_list("state", flat=True)), [BotChatMessageRequestStates.SENT] * 2)

    def test_lost_result_times_out_without_blocking_glib_or_retrying(self):
        self.driver.execute_script("outcomes = ['unknown'];")
        request = self.create_request()
        self.controller.take_action_based_on_chat_message_requests_in_db()
        ran = []
        GLib.idle_add(lambda: ran.append(True))
        self.drain_callbacks()
        self.assertEqual(ran, [True])
        GLib.source_remove(self.controller.chat_message_timeout_source)
        self.controller.chat_message_send_timed_out(request.id)
        request.refresh_from_db()
        self.assertEqual(request.state, BotChatMessageRequestStates.FAILED)
        self.assertEqual(request.failure_data, {"error": "delivery_unconfirmed"})
        self.controller.finish_chat_message_request({"request_id": request.id, "status": "sent"})
        self.assertEqual(self.driver.execute_script("return submissions;"), 1)
        request.refresh_from_db()
        self.assertEqual(request.state, BotChatMessageRequestStates.FAILED)

    def test_dispatch_exception_is_not_sent_or_logged_with_chat_text(self):
        request = self.create_request()
        self.adapter.driver = Mock()
        self.adapter.driver.execute_script.side_effect = RuntimeError("test error containing private chat text")
        with self.assertLogs("bots.bot_controller.bot_controller", level="WARNING") as logs:
            self.controller.take_action_based_on_chat_message_requests_in_db()
        request.refresh_from_db()
        self.assertEqual(request.state, BotChatMessageRequestStates.FAILED)
        self.assertEqual(request.failure_data, {"error": "dispatch_error"})
        self.assertNotIn("private chat text", " ".join(logs.output))

    def test_result_is_processed_even_while_recording_is_paused(self):
        self.adapter.recording_paused = True
        self.adapter.record_chat_messages_when_paused = False
        self.driver.execute_script("outcomes = ['confirmed'];")
        request = self.create_request()
        self.controller.take_action_based_on_chat_message_requests_in_db()
        self.deliver_result()
        request.refresh_from_db()
        self.assertEqual(request.state, BotChatMessageRequestStates.SENT)

    def test_synchronous_adapters_report_completion(self):
        for adapter_class in (TeamsBotAdapter, ZoomBotAdapter, ZoomWebBotAdapter):
            with self.subTest(adapter=adapter_class.__name__):
                adapter = adapter_class.__new__(adapter_class)
                adapter.ready_to_send_chat_messages = True
                adapter.driver = Mock()
                adapter.chat_ctrl = Mock()
                adapter.send_message_callback = self.controller.on_message_from_adapter
                self.controller.adapter = adapter
                request = self.create_request()
                self.controller.take_action_based_on_chat_message_requests_in_db()
                self.drain_callbacks()
                request.refresh_from_db()
                self.assertEqual(request.state, BotChatMessageRequestStates.SENT)

    def test_identical_incoming_text_does_not_hide_own_confirmation(self):
        self.driver.execute_script("insertMatchingIncomingMessage = true; outcomes = ['confirmed'];")
        request = self.create_request()
        self.controller.take_action_based_on_chat_message_requests_in_db()
        self.assertEqual(self.deliver_result()["status"], "sent")
        request.refresh_from_db()
        self.assertEqual(request.state, BotChatMessageRequestStates.SENT)

    def test_stopping_does_not_start_the_next_queued_message(self):
        self.driver.execute_script("outcomes = ['unknown'];")
        first = self.create_request()
        second = self.create_request()
        self.controller.take_action_based_on_chat_message_requests_in_db()
        self.controller.cleanup_called = True
        self.controller.finish_chat_message_request({"request_id": first.id, "status": "failed", "error": "bot_stopped"})
        self.drain_callbacks()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, BotChatMessageRequestStates.FAILED)
        self.assertEqual(first.failure_data, {"error": "bot_stopped"})
        self.assertEqual(second.state, BotChatMessageRequestStates.ENQUEUED)
        self.assertEqual(self.driver.execute_script("return submissions;"), 1)
