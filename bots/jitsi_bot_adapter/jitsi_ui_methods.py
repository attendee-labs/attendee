import logging
import sys
import time

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from bots.web_bot_adapter.ui_methods import (
    UiCouldNotClickElementException,
    UiCouldNotJoinMeetingWaitingForHostException,
    UiCouldNotJoinMeetingWaitingRoomTimeoutException,
    UiCouldNotLocateElementException,
    UiIncorrectPasswordException,
    UiMeetingNotFoundException,
    UiRequestToJoinDeniedException,
)

logger = logging.getLogger(__name__)

# The prejoin testids are identical on vanilla Jitsi, kMeet and Hostpoint Meet
PREJOIN_NAME_INPUT_SELECTOR = '[data-testid="prejoin.screen"] input'
PREJOIN_JOIN_BUTTON_SELECTOR = '[data-testid="prejoin.joinMeeting"]'
# kMeet-only: "join from browser / from the app" chooser shown on first visit in a fresh
# profile. Matched via the icon testid, which is language-independent.
BROWSER_CHOICE_BUTTON_SELECTOR = 'button:has(svg[data-testid="WebIcon"])'

# Join failure signals, verified against kMeet:
# - lobby enabled -> CONFERENCE_FAILED conference.connectionError.membersOnly, features/lobby.knocking = true
# - lobby request denied -> CONFERENCE_FAILED conference.connectionError.accessDenied
# - locked room -> CONFERENCE_FAILED conference.passwordRequired (repeats after a wrong password)
# - room.join(password) joins a locked room and also bypasses an enabled lobby
JOIN_STATE_RECORDER_SCRIPT = """
window.__attendeeJoinFailures = [];
const poll = setInterval(() => {
    const room = window.APP?.conference?._room;
    if (!room || !window.JitsiMeetJS) return;
    clearInterval(poll);
    room.on(window.JitsiMeetJS.events.conference.CONFERENCE_FAILED,
        (reason) => window.__attendeeJoinFailures.push(String(reason)));
}, 200);
"""

JOIN_STATE_QUERY_SCRIPT = """
const state = window.APP?.store?.getState() || {};
return {
    joined: window.APP?.conference?._room?.isJoined() === true,
    knocking: (state['features/lobby'] || {}).knocking === true,
    passwordRequired: !!(state['features/base/conference'] || {}).passwordRequired,
    failures: window.__attendeeJoinFailures || [],
};
"""

# Grace period after submitting a password before treating a persisting
# passwordRequired state as "the configured password was rejected"
PASSWORD_RESPONSE_GRACE_SECONDS = 10


class JitsiUIMethods:
    def locate_element(self, step, condition, wait_time_seconds=60):
        try:
            element = WebDriverWait(self.driver, wait_time_seconds).until(condition)
            return element
        except Exception as e:
            logger.info(f"Exception raised in locate_element for {step}")
            raise UiCouldNotLocateElementException(f"Exception raised in locate_element for {step}", step, e)

    def find_element_by_selector(self, selector_type, selector):
        try:
            return self.driver.find_element(selector_type, selector)
        except NoSuchElementException:
            return None
        except Exception as e:
            logger.info(f"Unknown error occurred in find_element_by_selector. Exception type = {type(e)}")
            return None

    def click_element(self, element, step):
        try:
            element.click()
        except Exception as e:
            logger.warning(f"Error occurred when clicking element {step}, will retry. Error: {e}")
            raise UiCouldNotClickElementException("Error occurred when clicking element", step, e)

    def check_if_jitsi_app_loaded(self):
        # Every Jitsi web app exposes window.APP. If it never shows up, the URL does not
        # point at a Jitsi deployment (wrong domain, 404 page, server down).
        def app_exists(driver):
            return driver.execute_script("return typeof window.APP !== 'undefined';")

        try:
            WebDriverWait(self.driver, 15).until(app_exists)
        except Exception as e:
            logger.warning("window.APP did not appear — page does not look like a Jitsi deployment")
            raise UiMeetingNotFoundException("Page did not load the Jitsi app", "jitsi_app_loaded", e)

    def click_browser_choice_button_if_present(self):
        # Wait until either the prejoin screen or the kMeet app-choice screen shows up;
        # dismiss the latter by choosing the browser. Vanilla Jitsi and Hostpoint Meet
        # go straight to the prejoin, so this loop just falls through there.
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.find_element_by_selector(By.CSS_SELECTOR, PREJOIN_NAME_INPUT_SELECTOR):
                return
            browser_choice_button = self.find_element_by_selector(By.CSS_SELECTOR, BROWSER_CHOICE_BUTTON_SELECTOR)
            if browser_choice_button:
                logger.info("Dismissing the kMeet app choice screen by choosing the browser")
                self.click_element(browser_choice_button, "browser_choice_button")
                return
            time.sleep(0.5)

    def fill_out_name_input(self):
        # kMeet prefills the name from localStorage into a React/MUI input where
        # element.clear() does not reliably fire a change event — clear via keyboard
        # and verify the resulting value instead. The element is re-located on every
        # attempt because React can remount the input between renders, which makes a
        # previously held reference stale.
        select_all_modifier = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
        for attempt in range(5):
            name_input = self.locate_element(
                step="name_input",
                condition=EC.element_to_be_clickable((By.CSS_SELECTOR, PREJOIN_NAME_INPUT_SELECTOR)),
                wait_time_seconds=30 if attempt == 0 else 5,
            )
            try:
                name_input.send_keys(select_all_modifier, "a")
                name_input.send_keys(Keys.DELETE)
                name_input.send_keys(self.display_name)
                time.sleep(0.5)
                if name_input.get_attribute("value") == self.display_name:
                    return
            except StaleElementReferenceException:
                logger.warning(f"Name input went stale during attempt {attempt + 1}, re-locating")
                continue
            logger.warning(f"Name input value mismatch after attempt {attempt + 1}, retrying")
        raise UiCouldNotLocateElementException("Could not fill out the name input with the bot name", "name_input")

    def submit_room_password(self):
        logger.info("Submitting the configured room password via the app api")
        self.driver.execute_script("window.APP.conference._room.join(arguments[0]);", self.jitsi_room_password)

    def wait_for_conference_joined(self):
        password_attempted_at = None
        waiting_room_first_seen_at = None
        knocking_ended_at = None
        started_at = time.time()
        waiting_room_timeout = self.automatic_leave_configuration.waiting_room_timeout_seconds
        wait_for_host_timeout = self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds
        num_attempts = int(max(waiting_room_timeout, wait_for_host_timeout)) * 2 + 120

        for _ in range(num_attempts):
            state = self.driver.execute_script(JOIN_STATE_QUERY_SCRIPT)
            failures = state["failures"]

            if state["joined"]:
                logger.info("Jitsi conference joined")
                return

            if any("accessDenied" in failure for failure in failures):
                logger.warning("Lobby request was denied by a moderator")
                raise UiRequestToJoinDeniedException("Lobby request was denied by a moderator", "waiting_room")

            if state["passwordRequired"]:
                if not self.jitsi_room_password:
                    raise UiIncorrectPasswordException("Room requires a password but none was configured", "room_password")
                if password_attempted_at is None:
                    password_attempted_at = time.time()
                    self.submit_room_password()
                elif time.time() - password_attempted_at > PASSWORD_RESPONSE_GRACE_SECONDS:
                    raise UiIncorrectPasswordException("Room rejected the configured password", "room_password")

            elif state["knocking"]:
                knocking_ended_at = None
                if waiting_room_first_seen_at is None:
                    waiting_room_first_seen_at = time.time()
                    logger.info("Bot is in the Jitsi lobby, waiting to be admitted")
                    if self.jitsi_room_password and password_attempted_at is None:
                        # The room password also bypasses the lobby. A wrong password just
                        # returns the bot to the lobby, where a moderator can still admit it.
                        password_attempted_at = time.time()
                        self.submit_room_password()
                if time.time() - waiting_room_first_seen_at > waiting_room_timeout:
                    if len(self.participants_info) > 1:
                        logger.warning("Waiting room timeout exceeded, but there is more than one participant in the meeting. Not aborting join attempt.")
                    else:
                        self.abort_join_attempt()
                        logger.warning("Waiting room timeout exceeded")
                        raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", "waiting_room")

            elif waiting_room_first_seen_at is not None:
                # Knocking ended without a join. Either the bot was admitted (the join
                # completes within a few seconds) or the request was denied — the state
                # check keeps working even if the CONFERENCE_FAILED event was missed.
                if knocking_ended_at is None:
                    knocking_ended_at = time.time()
                elif time.time() - knocking_ended_at > 15:
                    logger.warning("Lobby knocking ended without a join — treating as denied")
                    raise UiRequestToJoinDeniedException("Lobby request was denied by a moderator", "waiting_room")

            elif any("authenticationRequired" in failure for failure in failures):
                # Vanilla jitsi deployments (e.g. meet.jit.si) require an authenticated
                # moderator to start the room; guests wait until then. Not observed on
                # kMeet/Hostpoint, which allow anonymous room creation.
                if time.time() - started_at > wait_for_host_timeout:
                    raise UiCouldNotJoinMeetingWaitingForHostException("Timed out waiting for a moderator to start the meeting", "waiting_for_host")

            time.sleep(0.5)

        raise UiCouldNotLocateElementException("Timed out waiting for the Jitsi conference to be joined", "conference_joined")

    def attempt_to_join_meeting(self):
        # startWithAudioMuted/startWithVideoMuted are jitsi config overrides carried in the
        # URL fragment — media stays off from the start, no DOM interaction needed. The
        # stored meeting_url is normalized without fragments, so this cannot double up.
        self.driver.get(self.meeting_url + "#config.startWithAudioMuted=true&config.startWithVideoMuted=true")

        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": self.meeting_url,
                "permissions": ["audioCapture", "videoCapture"],
            },
        )

        self.check_if_jitsi_app_loaded()

        self.click_browser_choice_button_if_present()

        self.fill_out_name_input()

        # Arm the failure recorder after the app-choice step (which reloads the page and
        # would discard it) but before clicking join, so no CONFERENCE_FAILED is missed
        self.driver.execute_script(JOIN_STATE_RECORDER_SCRIPT)

        join_button = self.locate_element(
            step="join_button",
            condition=EC.element_to_be_clickable((By.CSS_SELECTOR, PREJOIN_JOIN_BUTTON_SELECTOR)),
            wait_time_seconds=15,
        )
        logger.info("Clicking the join button...")
        self.click_element(join_button, "join_button")

        self.wait_for_conference_joined()

        self.ready_to_show_bot_image()

    def click_leave_button(self):
        # Leave via the app API — no DOM dependency
        self.driver.execute_script("window.APP?.conference?.hangup?.();")
