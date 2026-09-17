import logging
import time

from django.utils import timezone

from bots.bot_adapter import ChatMessageSendError
from bots.models import BotChatMessageRequestManager, BotChatMessageRequestStates, BotEventManager

logger = logging.getLogger(__name__)


class ChatMessageSender:
    """Drain one bot's durable chat queue on its main loop without blocking it."""

    MAX_ATTEMPTS = 5
    RETRY_DEADLINE_SECONDS = 60
    RETRY_BASE_SECONDS = 2
    RETRY_MAX_SECONDS = 16

    def __init__(self, bot):
        self.bot = bot
        self.pending = False
        self.stopped = False
        self.next_attempt_at = 0
        self.request_id = None
        self.attempts = 0
        self.first_attempt_at = None
        self.last_error = None

    def send_pending(self, adapter):
        self.pending = True
        self.process(adapter)

    def stop(self):
        self.stopped = True
        self.pending = False
        self.bot.chat_message_requests.filter(state=BotChatMessageRequestStates.ENQUEUED).update(
            state=BotChatMessageRequestStates.FAILED,
            failure_data={"reason": "bot_stopped"},
            updated_at=timezone.now(),
        )

    def fail(self, request, reason):
        failure_data = {"reason": reason, "error_code": self.last_error, "attempts": self.attempts}
        BotChatMessageRequestManager.set_chat_message_request_failed(request, failure_data)
        logger.warning("Chat message failed bot=%s request=%s failure=%s", self.bot.object_id, request.id, failure_data)

    def process(self, adapter):
        if self.stopped or not self.pending or time.monotonic() < self.next_attempt_at:
            return
        if not BotEventManager.is_state_that_can_play_media(self.bot.state) or not adapter.is_ready_to_send_chat_messages():
            return

        requests = self.bot.chat_message_requests.filter(state=BotChatMessageRequestStates.ENQUEUED).order_by("created_at", "id")
        for request in requests:
            now = time.monotonic()
            if now < self.next_attempt_at:
                return
            if request.id != self.request_id:
                self.request_id = request.id
                self.attempts = 0
                self.first_attempt_at = now
                self.last_error = None
            if now - self.first_attempt_at >= self.RETRY_DEADLINE_SECONDS:
                self.fail(request, "retry_deadline_exceeded")
                continue

            self.attempts += 1
            try:
                adapter.send_chat_message(text=request.message, to_user_uuid=request.to_user_uuid)
            except ChatMessageSendError as exc:
                self.last_error = exc.code
                now = time.monotonic()
                self.next_attempt_at = now + adapter.CHAT_MESSAGE_INTERVAL_SECONDS
                if exc.retryable and self.attempts < self.MAX_ATTEMPTS and now - self.first_attempt_at < self.RETRY_DEADLINE_SECONDS:
                    delay = max(adapter.CHAT_MESSAGE_INTERVAL_SECONDS, min(self.RETRY_BASE_SECONDS * 2 ** (self.attempts - 1), self.RETRY_MAX_SECONDS))
                    self.next_attempt_at = max(now + adapter.CHAT_MESSAGE_INTERVAL_SECONDS, min(now + delay, self.first_attempt_at + self.RETRY_DEADLINE_SECONDS))
                    logger.warning("Chat message retry scheduled bot=%s request=%s attempt=%s error=%s", self.bot.object_id, request.id, self.attempts, exc.code)
                    return
                self.fail(request, "retry_exhausted" if exc.retryable else "send_rejected")
            else:
                self.next_attempt_at = time.monotonic() + adapter.CHAT_MESSAGE_INTERVAL_SECONDS
                BotChatMessageRequestManager.set_chat_message_request_sent(request)
        self.pending = False
