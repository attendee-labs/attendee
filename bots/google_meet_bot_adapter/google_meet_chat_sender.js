// A UI submission is optimistic: only a server-originated chat echo confirms delivery.
class GoogleMeetChatSender {
    static ATTEMPT_TIMEOUT_MS = 10000;
    static RETRY_DELAY_MS = 1000;
    static POLL_INTERVAL_MS = 100;
    static MAX_ATTEMPTS = 3;

    constructor(ws, userManager) {
        this.ws = ws;
        this.userManager = userManager;
        this.active = null;
        this.receivedMessageIds = new Set();
    }

    acknowledge(message) {
        const seen = this.receivedMessageIds.has(message.messageId);
        this.receivedMessageIds.add(message.messageId);
        if (this.receivedMessageIds.size > 100) {
            this.receivedMessageIds.delete(this.receivedMessageIds.values().next().value);
        }
        if (!seen && message.messageId && this.active && this.userManager.currentUserId &&
            message.deviceId === this.userManager.currentUserId &&
            message.chatMessageContent?.text === this.active.text) {
            this.active.acknowledgedMessageIds.add(String(message.messageId));
        }
    }

    newMessages(delivery) {
        return Array.from(document.querySelectorAll('[data-message-id]')).filter(element =>
            !delivery.previousMessageIds.has(element.dataset.messageId) &&
            element.textContent.replace(/\s+/g, ' ').trim() === delivery.text.replace(/\s+/g, ' ').trim());
    }

    isAcknowledged(delivery) {
        return this.newMessages(delivery).some(message => delivery.acknowledgedMessageIds.has(message.dataset.messageId));
    }

    // Only the Resend control belonging to this new message may be clicked.
    failedMessageControl(delivery) {
        for (const message of this.newMessages(delivery)) {
            for (let parent = message.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
                if (parent.querySelectorAll('[data-message-id]').length !== 1) break;
                const resend = parent.querySelector('button[aria-label="Resend"]');
                if (resend && parent.textContent.includes("Your message wasn't delivered")) return resend;
            }
        }
        return null;
    }

    async send(text, requestId) {
        if (this.active) {
            this.ws.sendJson({type: 'ChatMessageSendResult', request_id: requestId, status: 'failed', error: 'send_in_progress', attempts: 0});
            return;
        }
        const delivery = {
            text,
            acknowledgedMessageIds: new Set(),
            previousMessageIds: new Set(Array.from(document.querySelectorAll('[data-message-id]'), element => element.dataset.messageId)),
        };
        this.active = delivery;
        let attempts = 0;
        let error = 'delivery_unconfirmed';
        let previousResend = null;
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        try {
            const input = document.querySelector('textarea[aria-label="Send a message"]');
            if (!input || input.disabled) {
                error = 'chat_input_unavailable';
                return;
            }
            input.focus();
            input.value = text;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            attempts = 1;
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
            while (true) {
                const deadline = performance.now() + GoogleMeetChatSender.ATTEMPT_TIMEOUT_MS;
                let resend = null;
                let failureCleared = previousResend === null;
                while (!this.isAcknowledged(delivery) && performance.now() < deadline) {
                    resend = this.failedMessageControl(delivery);
                    if (!resend || resend.disabled || resend !== previousResend) failureCleared = true;
                    if (resend && !resend.disabled && failureCleared) break;
                    resend = null;
                    await sleep(GoogleMeetChatSender.POLL_INTERVAL_MS);
                }
                if (this.isAcknowledged(delivery)) return;
                // No acknowledgement and no explicit failure: do not create duplicates.
                if (!resend) return;
                error = 'delivery_failed';
                if (attempts >= GoogleMeetChatSender.MAX_ATTEMPTS) return;
                await sleep(GoogleMeetChatSender.RETRY_DELAY_MS * attempts);
                if (this.isAcknowledged(delivery)) return;
                resend = this.failedMessageControl(delivery);
                if (!resend || resend.disabled) {
                    error = 'delivery_unconfirmed';
                    return;
                }
                attempts += 1;
                previousResend = resend;
                resend.click();
                error = 'delivery_unconfirmed';
                // Let Meet replace the failed state before examining it again.
                await sleep(GoogleMeetChatSender.POLL_INTERVAL_MS);
            }
        } catch {
            // Do not include the chat text or URL credentials in diagnostics.
            error = 'send_error';
        } finally {
            const acknowledged = this.isAcknowledged(delivery);
            this.active = null;
            this.ws.sendJson({
                type: 'ChatMessageSendResult', request_id: requestId,
                status: acknowledged ? 'sent' : 'failed',
                error: acknowledged ? null : error, attempts,
            });
        }
    }
}
window.GoogleMeetChatSender = GoogleMeetChatSender;
