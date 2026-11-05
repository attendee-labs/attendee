import logging
import time

from bots.web_bot_adapter.ui_methods import (
    UiBlockedByCaptchaException,
    UiCouldNotJoinMeetingWaitingForHostException,
    UiCouldNotJoinMeetingWaitingRoomTimeoutException,
    UiIncorrectPasswordException,
    UiMeetingNotFoundException,
    UiRequestToJoinDeniedException,
    UiRetryableException,
)

from .webex_static_server import start_webex_static_server

logger = logging.getLogger(__name__)

# The page reports why joining failed as one of these methods. Anything else, such as a dropped network
# connection, is retried.
JOIN_FAILURE_EXCEPTIONS = {
    "meeting_not_found": UiMeetingNotFoundException,
    "incorrect_password": UiIncorrectPasswordException,
    "blocked_by_captcha": UiBlockedByCaptchaException,
    "removed_from_waiting_room": UiRequestToJoinDeniedException,
}

JOIN_STEP = "wait_to_be_admitted_to_meeting"


class WebexUIMethods:
    """Joins a meeting through the Webex SDK in webex_chromedriver_page.js, which reports its progress in window.webexJoinState."""

    def attempt_to_join_meeting(self):
        self.failed_to_join_reason = None

        port = start_webex_static_server()
        page_url = f"http://127.0.0.1:{port}/webex_chromedriver_page.html"
        logger.info(f"Serving Webex SDK HTML from {page_url}")
        self.driver.get(page_url)

        self.wait_to_be_admitted_to_meeting()
        self.ready_to_show_bot_image()

    def wait_to_be_admitted_to_meeting(self):
        logger.info("Waiting to be admitted to the meeting...")
        timeout_started_at = time.time()
        was_waiting_for_host = False
        previous_phase = None

        while True:
            join_state = self.read_join_state()
            self.raise_if_failed_to_join()
            if join_state["phase"] != previous_phase:
                logger.info(f"Webex join phase is now {join_state['phase']}")
                previous_phase = join_state["phase"]
            if join_state["phase"] == "joined":
                logger.info("We have been admitted to the meeting")
                return

            if join_state["phase"] == "lobby":
                self.send_bot_put_in_waiting_room_message()

            # The host's meeting not having started looks like a normal lobby, except nobody else is in it
            is_waiting_for_host = join_state["phase"] == "lobby" and not join_state["othersInMeeting"]
            if is_waiting_for_host != was_waiting_for_host:
                logger.info(f"is_waiting_for_host_to_start_meeting changed from {was_waiting_for_host} to {is_waiting_for_host}. Resetting timeout")
                timeout_started_at = time.time()
                was_waiting_for_host = is_waiting_for_host

            self.check_if_timeout_exceeded(timeout_started_at, is_waiting_for_host)
            time.sleep(1)

    def read_join_state(self):
        try:
            return self.driver.execute_script("return window.webexJoinState")
        except Exception as e:
            raise UiRetryableException("Could not read the join state of the Webex page", JOIN_STEP, e)

    def raise_if_failed_to_join(self):
        reason = self.failed_to_join_reason
        if reason:
            exception_class = JOIN_FAILURE_EXCEPTIONS.get(reason.get("method"), UiRetryableException)
            logger.info(f"Webex join failed with reason {reason}. Raising {exception_class.__name__}")
            raise exception_class(f"Webex join failed: {reason}", JOIN_STEP)

    def check_if_timeout_exceeded(self, timeout_started_at, is_waiting_for_host):
        leave_configuration = self.automatic_leave_configuration
        timeout_seconds = leave_configuration.wait_for_host_to_start_meeting_timeout_seconds if is_waiting_for_host else leave_configuration.waiting_room_timeout_seconds
        if time.time() - timeout_started_at <= timeout_seconds:
            return

        self.click_leave_button()
        self.abort_join_attempt()
        if is_waiting_for_host:
            logger.info("Waiting for host to start meeting timeout exceeded. Raising UiCouldNotJoinMeetingWaitingForHostException")
            raise UiCouldNotJoinMeetingWaitingForHostException("Waiting for host to start meeting timeout exceeded", JOIN_STEP)
        logger.info("Waiting room timeout exceeded. Raising UiCouldNotJoinMeetingWaitingRoomTimeoutException")
        raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", JOIN_STEP)

    def click_leave_button(self):
        self.driver.execute_async_script("const done = arguments[arguments.length - 1]; window.leaveMeeting().then(done);")
