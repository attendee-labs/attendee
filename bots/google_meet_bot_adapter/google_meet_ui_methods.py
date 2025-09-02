import logging
import os
import time

from selenium.common.exceptions import ElementNotInteractableException, NoSuchElementException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from bots.models import RecordingViews
from bots.web_bot_adapter.ui_methods import UiCouldNotClickElementException, UiCouldNotJoinMeetingWaitingForHostException, UiCouldNotJoinMeetingWaitingRoomTimeoutException, UiCouldNotLocateElementException, UiLoginRequiredException, UiMeetingNotFoundException, UiRequestToJoinDeniedException, UiRetryableExpectedException

logger = logging.getLogger(__name__)


class UiGoogleBlockingUsException(UiRetryableExpectedException):
    def __init__(self, message, step=None, inner_exception=None):
        super().__init__(message, step, inner_exception)


class GoogleMeetUIMethods:
    def locate_element(self, step, condition, wait_time_seconds=60):
        try:
            element = WebDriverWait(self.driver, wait_time_seconds).until(condition)
            return element
        except Exception as e:
            # Take screenshot when any exception occurs
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

    def click_element_and_handle_blocking_elements(self, element, step):
        num_attempts = 30

        for attempt_index in range(num_attempts):
            try:
                self.click_element(element, step)
                return
            except UiCouldNotClickElementException as e:
                logger.info(f"Error occurred when clicking element for step {step}, will click any blocking elements and retry the click")
                self.click_others_may_see_your_meeting_differently_button(step)
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e

    # Do it via javascript to avoid the element not being interactable exception
    def click_element_forcefully(self, element, step):
        try:
            self.driver.execute_script("arguments[0].click();", element)
        except Exception as e:
            logger.info(f"Error occurred when forcefully clicking element for step {step}, will retry")
            raise UiCouldNotClickElementException("Error occurred when forcefully clicking element", step, e)

    def click_element(self, element, step):
        try:
            element.click()
        except Exception as e:
            logger.info(f"Error occurred when clicking element for step {step}, will retry")
            raise UiCouldNotClickElementException("Error occurred when clicking element", step, e)

    # If the meeting you're about to join is being recorded, gmeet makes you click an additional button after you're admitted to the meeting
    def click_this_meeting_is_being_recorded_join_now_button(self, step):
        this_meeting_is_being_recorded_join_now_button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Join now"]]')
        if this_meeting_is_being_recorded_join_now_button:
            logger.info("Clicking this_meeting_is_being_recorded_join_now_button")
            self.click_element(this_meeting_is_being_recorded_join_now_button, step)

    # Some modal that google put up
    def click_others_may_see_your_meeting_differently_button(self, step):
        others_may_see_your_meeting_differently_button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Got it"]]')
        if others_may_see_your_meeting_differently_button:
            logger.info("Clicking others_may_see_your_meeting_differently_button")
            self.click_element_forcefully(others_may_see_your_meeting_differently_button, step)

    def look_for_blocked_element(self, step):
        cannot_join_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "You can\'t join this video call") or contains(text(), "There is a problem connecting to this video call")]')
        if cannot_join_element:
            # This means google is blocking us for whatever reason, but we can retry
            logger.info("Google is blocking us for whatever reason, but we can retry. Raising UiGoogleBlockingUsException")
            raise UiGoogleBlockingUsException("You can't join this video call", step)

    def look_for_login_required_element(self, step):
        login_required_element = self.find_element_by_selector(By.XPATH, '//h1[contains(., "Sign in")]/parent::*[.//*[contains(text(), "your Google Account")]]')
        if login_required_element:
            logger.info("Login required. Raising UiLoginRequiredException")
            raise UiLoginRequiredException("Login required", step)

    def look_for_denied_your_request_element(self, step):
        denied_your_request_element = self.find_element_by_selector(
            By.XPATH,
            '//*[contains(text(), "Someone in the call denied your request to join") or contains(text(), "No one responded to your request to join the call")]',
        )
        if denied_your_request_element:
            logger.info("Someone in the call denied our request to join. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("Someone in the call denied your request to join", step)

    def look_for_asking_to_be_let_in_element_after_waiting_period_expired(self, step):
        asking_to_be_let_in_element = self.find_element_by_selector(
            By.XPATH,
            '//*[contains(text(), "Asking to be let in")]',
        )
        if asking_to_be_let_in_element:
            logger.info("Bot was not let in after waiting period expired. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("Bot was not let in after waiting period expired", step)

    def check_if_waiting_room_timeout_exceeded(self, waiting_room_timeout_started_at, step):
        waiting_room_timeout_exceeded = time.time() - waiting_room_timeout_started_at > self.automatic_leave_configuration.waiting_room_timeout_seconds
        if waiting_room_timeout_exceeded:
            # If there is more than one participant in the meeting, then the bot was just let in and we should not timeout
            if len(self.participants_info) > 1:
                logger.info("Waiting room timeout exceeded, but there is more than one participant in the meeting. Not aborting join attempt.")
                return
            self.abort_join_attempt()
            logger.info("Waiting room timeout exceeded. Raising UiCouldNotJoinMeetingWaitingRoomTimeoutException")
            raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", step)

    def turn_off_media_inputs(self):
        logger.info("Waiting for the microphone button...")
        MICROPHONE_BUTTON_SELECTOR = 'div[aria-label="Turn off microphone"]'
        microphone_button = self.locate_element(
            step="turn_off_microphone_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, MICROPHONE_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the microphone button...")
        self.click_element(microphone_button, "turn_off_microphone_button")

        logger.info("Waiting for the camera button...")
        CAMERA_BUTTON_SELECTOR = 'div[aria-label="Turn off camera"]'
        camera_button = self.locate_element(
            step="turn_off_camera_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, CAMERA_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the camera button...")
        self.click_element(camera_button, "turn_off_camera_button")

    def fill_out_name_input(self):
        num_attempts_to_look_for_name_input = 30
        logger.info("Waiting for the name input field...")
        for attempt_to_look_for_name_input_index in range(num_attempts_to_look_for_name_input):
            try:
                name_input = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="text"][aria-label="Your name"]')))
                logger.info("name input found")
                name_input.send_keys(self.display_name)
                return
            except TimeoutException as e:
                self.look_for_blocked_element("name_input")
                self.look_for_login_required_element("name_input")

                last_check_timed_out = attempt_to_look_for_name_input_index == num_attempts_to_look_for_name_input - 1
                if last_check_timed_out:
                    logger.info("Could not find name input. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find name input. Timed out.", "name_input", e)

            except ElementNotInteractableException as e:
                logger.info("Name input is not interactable. Going to try again.")
                last_check_non_interactable = attempt_to_look_for_name_input_index == num_attempts_to_look_for_name_input - 1
                if last_check_non_interactable:
                    logger.info("Could not find name input. Non interactable. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find name input. Non interactable.", "name_input", e)

            except Exception as e:
                logger.info(f"Could not find name input. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException("Could not find name input. Unknown error.", "name_input", e)

    def click_captions_button(self):
        num_attempts_to_look_for_captions_button = 600
        logger.info("Waiting for captions button...")
        waiting_room_timeout_started_at = time.time()
        for attempt_to_look_for_captions_button_index in range(num_attempts_to_look_for_captions_button):
            try:
                captions_button = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Turn on captions"]')))
                logger.info("Captions button found")
                self.click_element(captions_button, "click_captions_button")
                return
            except UiCouldNotClickElementException as e:
                self.click_this_meeting_is_being_recorded_join_now_button("click_captions_button")
                self.click_others_may_see_your_meeting_differently_button("click_captions_button")
                last_check_could_not_click_element = attempt_to_look_for_captions_button_index == num_attempts_to_look_for_captions_button - 1
                if last_check_could_not_click_element:
                    logger.info("Could not click captions button. Raising UiCouldNotClickElementException")
                    raise e
            except TimeoutException as e:
                self.look_for_blocked_element("click_captions_button")
                self.look_for_denied_your_request_element("click_captions_button")
                self.click_this_meeting_is_being_recorded_join_now_button("click_captions_button")
                self.click_others_may_see_your_meeting_differently_button("click_captions_button")
                self.check_if_waiting_room_timeout_exceeded(waiting_room_timeout_started_at, "click_captions_button")

                last_check_timed_out = attempt_to_look_for_captions_button_index == num_attempts_to_look_for_captions_button - 1
                if last_check_timed_out:
                    self.look_for_asking_to_be_let_in_element_after_waiting_period_expired("click_captions_button")

                    logger.info("Could not find captions button. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException(
                        "Could not find captions button. Timed out.",
                        "click_captions_button",
                        e,
                    )

            except Exception as e:
                logger.info(f"Could not find captions button. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException(
                    "Could not find captions button. Unknown error.",
                    "click_captions_button",
                    e,
                )

    def check_if_meeting_is_found(self):
        meeting_not_found_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Check your meeting code") or contains(text(), "Invalid video call name") or contains(text(), "Your meeting code has expired")]')
        if meeting_not_found_element:
            logger.info("Meeting not found. Raising UiMeetingNotFoundException")
            raise UiMeetingNotFoundException("Meeting not found", "check_if_meeting_is_found")

    def wait_for_host_if_needed(self):
        host_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Waiting for the host to join")]')
        if host_element:
            # Wait for up to n seconds for the host to join
            wait_time_seconds = self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds
            logger.info(f"We must wait for the host to join before we can join the meeting. Waiting for {wait_time_seconds} seconds...")
            try:
                WebDriverWait(self.driver, wait_time_seconds).until(EC.invisibility_of_element_located((By.XPATH, '//*[contains(text(), "Waiting for the host to join")]')))
            except TimeoutException:
                logger.info("Host did not join the meeting in time. Raising UiCouldNotJoinMeetingWaitingForHostException")
                raise UiCouldNotJoinMeetingWaitingForHostException("Host did not join the meeting in time", "wait_for_host_if_needed")

    def get_layout_to_select(self):
        if self.recording_view == RecordingViews.SPEAKER_VIEW:
            return "sidebar"
        elif self.recording_view == RecordingViews.GALLERY_VIEW:
            return "tiled"
        elif self.recording_view == RecordingViews.SPEAKER_VIEW_NO_SIDEBAR:
            return "spotlight"
        else:
            return "sidebar"

    def turn_off_reactions(self):
        try:
            self.attempt_to_turn_off_reactions()
        except Exception as e:
            logger.info(f"Error turning off reactions: {e}")

    def attempt_to_turn_off_reactions(self):
        logger.info("Attempting to turn off reactions")
        logger.info("Waiting for the more options button...")
        MORE_OPTIONS_BUTTON_SELECTOR = 'button[jsname="NakZHc"][aria-label="More options"]'
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, MORE_OPTIONS_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            condition=EC.presence_of_element_located((By.XPATH, '//li[.//span[text()="Settings"]]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "settings_list_item")

        logger.info("Waiting for the reactions tab...")
        self.locate_element(
            step="reactions_tab",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Reactions"]')),
            wait_time_seconds=6,
        )

        # Use javascript to click the reactions button
        self.driver.execute_script("document.querySelector('button[aria-label=\"Show reactions from others\"]').click();")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Close dialog"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def set_layout(self, layout_to_select):
        num_attempts = 3
        for attempt_index in range(num_attempts):
            try:
                self.attempt_to_set_layout(layout_to_select)
                return
            except Exception as e:
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e
                logger.info(f"Error setting layout: {e}. Retrying. Attempt #{attempt_index}...")

    def attempt_to_set_layout(self, layout_to_select):
        logger.info("Begin setting layout. Waiting for the more options button...")
        MORE_OPTIONS_BUTTON_SELECTOR = 'button[jsname="NakZHc"][aria-label="More options"]'
        more_options_button = self.locate_element(
            step="more_options_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, MORE_OPTIONS_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the more options button....")
        self.click_element_and_handle_blocking_elements(more_options_button, "more_options_button")

        logger.info("Waiting for the 'Change layout' list item...")
        change_layout_list_item = self.locate_element(
            step="change_layout_item",
            condition=EC.presence_of_element_located((By.XPATH, '//li[.//span[text()="Change layout" or text()="Adjust view"] or @jsname="WZerud"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the 'Change layout' list item....")
        self.click_element_and_handle_blocking_elements(change_layout_list_item, "change_layout_list_item")

        if layout_to_select == "spotlight":
            logger.info("Waiting for the 'Spotlight' label element")
            spotlight_label = self.locate_element(
                step="spotlight_label",
                condition=EC.presence_of_element_located((By.XPATH, '//label[.//span[text()="Spotlight"]]')),
                wait_time_seconds=6,
            )
            logger.info("Clicking the 'Spotlight' label element")
            self.click_element(spotlight_label, "spotlight_label")

        if layout_to_select == "sidebar":
            logger.info("Waiting for the 'Sidebar' label element")
            sidebar_label = self.locate_element(
                step="sidebar_label",
                condition=EC.presence_of_element_located((By.XPATH, '//label[.//span[text()="Sidebar"]]')),
                wait_time_seconds=6,
            )
            logger.info("Clicking the 'Sidebar' label element")
            self.click_element(sidebar_label, "sidebar_label")

        if layout_to_select == "tiled":
            logger.info("Waiting for the 'Tiled' label element")
            tiled_label = self.locate_element(
                step="tiled_label",
                condition=EC.presence_of_element_located((By.XPATH, '//label[.//span[@class="xo15nd" and contains(text(), "Tiled")]]')),
                wait_time_seconds=6,
            )
            logger.info("Clicking the 'Tiled' label element")
            self.click_element(tiled_label, "tiled_label")

            logger.info("Waiting for the tile selector element")
            tile_selector = self.locate_element(
                step="tile_selector",
                condition=EC.presence_of_element_located((By.CSS_SELECTOR, ".ByPkaf")),
                wait_time_seconds=6,
            )

            logger.info("Finding all tile options")
            tile_options = tile_selector.find_elements(By.CSS_SELECTOR, ".gyG0mb-zD2WHb-SYOSDb-OWXEXe-mt1Mkb")

            if tile_options:
                logger.info("Clicking the last tile option (49 tiles)")
                last_tile_option = tile_options[-1]
                self.click_element(last_tile_option, "last_tile_option")
            else:
                logger.info("No tile options found")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, '[aria-modal="true"] button[aria-label="Close"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    # returns nothing if succeeded, raises an exception if failed
    def attempt_to_join_meeting(self):
        layout_to_select = self.get_layout_to_select()

        self.driver.get(self.meeting_url)

        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": self.meeting_url,
                "permissions": [
                    "geolocation",
                    "audioCapture",
                    "displayCapture",
                    "videoCapture",
                ],
            },
        )

        self.check_if_meeting_is_found()

        self.fill_out_name_input()

        self.turn_off_media_inputs()

        logger.info("Waiting for the 'Ask to join' or 'Join now' button...")
        join_button = self.locate_element(
            step="join_button",
            condition=EC.presence_of_element_located((By.XPATH, '//button[.//span[text()="Ask to join" or text()="Join now"]]')),
            wait_time_seconds=60,
        )
        logger.info("Clicking the join button...")
        self.click_element(join_button, "join_button")

        self.click_captions_button()

        self.wait_for_host_if_needed()

        self.set_layout(layout_to_select)

        if self.google_meet_closed_captions_language:
            self.select_language(self.google_meet_closed_captions_language)

        if os.getenv("DO_NOT_RECORD_MEETING_REACTIONS") == "true":
            self.turn_off_reactions()

        self.ready_to_show_bot_image()

    def scroll_element_into_view(self, element, step):
        try:
            actions = ActionChains(self.driver)
            actions.move_to_element(element).perform()
            logger.info(f"Scrolled element into view for {step}")
        except Exception as e:
            logger.info(f"Error scrolling element into view for {step}")
            raise UiCouldNotLocateElementException(
                "Error scrolling element into view",
                step,
                e,
            )

    def select_language(self, language):
        logger.info(f"Selecting language: {language}")
        logger.info("Waiting for the more options button...")
        MORE_OPTIONS_BUTTON_SELECTOR = 'button[jsname="NakZHc"][aria-label="More options"]'
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, MORE_OPTIONS_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            condition=EC.presence_of_element_located((By.XPATH, '//li[.//span[text()="Settings"]]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "settings_list_item")

        logger.info("Waiting for the captions button")
        self.locate_element(
            step="captions_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[jsname="z4Tpl"][aria-label="Captions"]')),
            wait_time_seconds=6,
        )

        # Uses javascript to select the language, bypassing the need for the dropdown to be visible
        click_language_option_result = self.driver.execute_script("return clickLanguageOption('{}')".format(language))
        logger.info(f"click_language_option_result: {click_language_option_result}")
        if not click_language_option_result:
            raise UiCouldNotLocateElementException(f"Could not find language option {language}", "language_option")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Close dialog"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def click_leave_button(self):
        logger.info("Waiting for the leave button")
        num_attempts = 5
        for attempt_index in range(num_attempts):
            leave_button = WebDriverWait(self.driver, 16).until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        'button[jsname="CQylAd"][aria-label="Leave call"]',
                    )
                )
            )
            logger.info("Clicking the leave button")
            try:
                leave_button.click()
                return
            except Exception as e:
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e
                logger.info("Error clicking leave button. Retrying...")
