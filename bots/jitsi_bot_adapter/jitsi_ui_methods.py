import logging

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from bots.web_bot_adapter.ui_methods import (
    UiCouldNotClickElementException,
    UiCouldNotLocateElementException,
)

logger = logging.getLogger(__name__)

# The prejoin testids are identical on vanilla Jitsi, kMeet and Hostpoint Meet
# (verified 2026-08-20, see attendee/SPIKE-jitsi-app-api.md in the deployment repo)
PREJOIN_NAME_INPUT_SELECTOR = '[data-testid="prejoin.screen"] input'
PREJOIN_JOIN_BUTTON_SELECTOR = '[data-testid="prejoin.joinMeeting"]'


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

    def fill_out_name_input(self):
        name_input = self.locate_element(
            step="name_input",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, PREJOIN_NAME_INPUT_SELECTOR)),
            wait_time_seconds=30,
        )
        # kMeet prefills the display name from localStorage — clear it before typing
        name_input.clear()
        name_input.send_keys(self.display_name)

    def wait_for_conference_joined(self, timeout_seconds=30):
        # The Jitsi web app exposes the lib-jitsi-meet conference object as APP.conference.
        # Waiting on the app API instead of the DOM keeps this independent of whitelabel UI changes.
        def conference_is_joined(driver):
            return driver.execute_script("return window.APP?.conference?._room?.isJoined() === true;")

        try:
            WebDriverWait(self.driver, timeout_seconds).until(conference_is_joined)
        except Exception as e:
            # ponytail: lobby / wrong password also end up here for now — phase 2 adds
            # dedicated detection and maps them to UiCouldNotJoinMeetingWaitingRoomTimeoutException etc.
            raise UiCouldNotLocateElementException("Timed out waiting for the Jitsi conference to be joined", "conference_joined", e)

    def attempt_to_join_meeting(self):
        self.driver.get(self.meeting_url)

        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": self.meeting_url,
                "permissions": ["audioCapture", "videoCapture"],
            },
        )

        self.fill_out_name_input()

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
