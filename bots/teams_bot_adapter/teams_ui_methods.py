import logging
import time

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from bots.models import RecordingViews
from bots.web_bot_adapter.ui_methods import UiCouldNotClickElementException, UiCouldNotJoinMeetingWaitingRoomTimeoutException, UiCouldNotLocateElementException, UiLoginAttemptFailedException, UiLoginRequiredException, UiMeetingNotFoundException, UiRequestToJoinDeniedException, UiRetryableExpectedException

logger = logging.getLogger(__name__)


class UiTeamsBlockingUsException(UiRetryableExpectedException):
    def __init__(self, message, step=None, inner_exception=None):
        super().__init__(message, step, inner_exception)


class TeamsUIMethods:
    def __init__(self, driver, meeting_url, display_name):
        self.driver = driver
        self.meeting_url = meeting_url
        self.display_name = display_name

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

    def look_for_waiting_to_be_admitted_element(self, step):
        waiting_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Someone will let you in soon")]')
        if waiting_element:
            # Check if we've been waiting too long
            logger.info("Still waiting to be admitted to the meeting after waiting period expired. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("Bot was not let in after waiting period expired", step)

    def turn_off_media_inputs(self):
        logger.info("Waiting for the microphone button...")
        microphone_button = self.locate_element(step="turn_off_microphone_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, '[data-tid="toggle-mute"]')), wait_time_seconds=60)
        logger.info("Clicking the microphone button...")
        self.click_element(microphone_button, "turn_off_microphone_button")

        logger.info("Waiting for the camera button...")
        camera_button = self.locate_element(step="turn_off_camera_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, '[data-tid="toggle-video"]')), wait_time_seconds=6)
        logger.info("Clicking the camera button...")
        # if the aria-checked attribute of the element is true, then click the element
        if camera_button.get_attribute("aria-checked") == "true" or camera_button.get_attribute("checked") == "true":
            self.click_element(camera_button, "turn_off_camera_button")
        else:
            logger.info("Camera button is already off, not clicking it")

    def join_now_button_is_present(self):
        join_button = self.find_element_by_selector(By.CSS_SELECTOR, '[data-tid="prejoin-join-button"]')
        if join_button:
            return True
        return False

    def fill_out_name_input(self):
        num_attempts = 30
        logger.info("Waiting for the name input field...")
        for attempt_index in range(num_attempts):
            try:
                name_input = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-tid="prejoin-display-name-input"]')))
                logger.info("Name input found")
                name_input.send_keys(self.display_name)
                return
            except TimeoutException as e:
                self.look_for_microsoft_login_form_element("name_input")

                if self.teams_bot_login_credentials and self.join_now_button_is_present():
                    logger.info("Join now button is present. Assuming name input is not present because we don't need to fill it out, so returning.")
                    return

                last_check_timed_out = attempt_index == num_attempts - 1
                if last_check_timed_out:
                    logger.info("Could not find name input. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find name input. Timed out.", "name_input", e)
            except Exception as e:
                logger.info(f"Could not find name input. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException("Could not find name input. Unknown error.", "name_input", e)

    def click_captions_button(self):
        logger.info("Enabling closed captions programatically...")
        closed_caption_enable_result = self.driver.execute_script("return window.callManager?.enableClosedCaptions()")
        if closed_caption_enable_result:
            logger.info("Closed captions enabled programatically")

            if self.teams_closed_captions_language:
                closed_caption_set_language_result = self.driver.execute_script("return window.callManager?.setClosedCaptionsLanguage(arguments[0]);", self.teams_closed_captions_language)
                if closed_caption_set_language_result:
                    logger.info("Closed captions language set programatically")
                else:
                    logger.error("Failed to set closed captions language programatically")

            return

        logger.info("Failed to enable closed captions programatically. Waiting for the Language and Speech button...")
        try:
            language_and_speech_button = self.locate_element(step="language_and_speech_button", condition=EC.presence_of_element_located((By.ID, "LanguageSpeechMenuControl-id")), wait_time_seconds=4)
            logger.info("Clicking the language and speech button...")
            self.click_element(language_and_speech_button, "language_and_speech_button")
        except Exception:
            logger.info("Unable to find language and speech button. Exception will be caught because the caption button may be directly visible instead.")

        logger.info("Waiting for the closed captions button...")
        closed_captions_button = self.locate_element(step="closed_captions_button", condition=EC.presence_of_element_located((By.ID, "closed-captions-button")), wait_time_seconds=10)
        logger.info("Clicking the closed captions button...")
        self.click_element(closed_captions_button, "closed_captions_button")

    def check_if_waiting_room_timeout_exceeded(self, waiting_room_timeout_started_at, step):
        waiting_room_timeout_exceeded = time.time() - waiting_room_timeout_started_at > self.automatic_leave_configuration.waiting_room_timeout_seconds
        if waiting_room_timeout_exceeded:
            # If there is more than one participant in the meeting, then the bot was just let in and we should not timeout
            if len(self.participants_info) > 1:
                logger.info("Waiting room timeout exceeded, but there is more than one participant in the meeting. Not aborting join attempt.")
                return

            try:
                self.click_cancel_join_button()
            except Exception:
                logger.info("Error clicking cancel join button, but not a fatal error")

            self.abort_join_attempt()
            logger.info("Waiting room timeout exceeded. Raising UiCouldNotJoinMeetingWaitingRoomTimeoutException")
            raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", step)

    def click_show_more_button(self):
        waiting_room_timeout_started_at = time.time()
        num_attempts = self.automatic_leave_configuration.waiting_room_timeout_seconds * 10
        logger.info("Waiting for the show more button...")
        for attempt_index in range(num_attempts):
            try:
                show_more_button = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.ID, "callingButtons-showMoreBtn")))
                logger.info("Clicking the show more button...")
                self.click_element(show_more_button, "click_show_more_button")
                return
            except TimeoutException:
                self.look_for_sign_in_required_element("click_show_more_button")
                self.look_for_denied_your_request_element("click_show_more_button")
                self.look_for_we_could_not_connect_you_element("click_show_more_button")

                self.check_if_waiting_room_timeout_exceeded(waiting_room_timeout_started_at, "click_show_more_button")

            except Exception as e:
                logger.info("Exception raised in locate_element for show_more_button")
                raise UiCouldNotLocateElementException("Exception raised in locate_element for click_show_more_button", "click_show_more_button", e)

    def look_for_sign_in_required_element(self, step):
        sign_in_required_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "We need to verify your info before you can join")]')
        if sign_in_required_element:
            logger.info("Sign in required. Raising UiLoginRequiredException")
            raise UiLoginRequiredException("Sign in required", step)

    def look_for_microsoft_login_form_element(self, step):
        # Check for Microsoft login form (email input)
        microsoft_login_element = self.find_element_by_selector(By.CSS_SELECTOR, 'input[name="loginfmt"][type="email"]')
        if microsoft_login_element:
            logger.info("Microsoft login form detected. Raising UiMeetingNotFoundException")
            raise UiMeetingNotFoundException("Microsoft login form detected", step)

    def look_for_we_could_not_connect_you_element(self, step):
        we_could_not_connect_you_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "we couldn\'t connect you")]')
        if we_could_not_connect_you_element:
            logger.info("Teams is blocking us for whatever reason, but we can retry. Raising UiTeamsBlockingUsException")
            raise UiTeamsBlockingUsException("Teams is blocking us for whatever reason, but we can retry", step)

    def look_for_denied_your_request_element(self, step):
        denied_your_request_element = self.find_element_by_selector(
            By.XPATH,
            '//*[contains(text(), "but you were denied access to the meeting") or contains(text(), "Your request to join was declined")]',
        )

        if denied_your_request_element:
            logger.info("Someone in the call denied our request to join. Raising UiRequestToJoinDeniedException")
            dismiss_button = self.locate_element(step="closed_captions_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, '[data-tid="calling-retry-cancelbutton"]')), wait_time_seconds=2)
            if dismiss_button:
                logger.info("Clicking the dismiss button...")
                self.click_element(dismiss_button, "dismiss_button")
            raise UiRequestToJoinDeniedException("Someone in the call denied your request to join", step)

    def set_layout(self, layout_to_select):
        logger.info("Waiting for the view button...")
        view_button = self.locate_element(step="view_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, "#view-mode-button, #custom-view-button")), wait_time_seconds=60)
        logger.info("Clicking the view button...")
        self.click_element(view_button, "view_button")

        if layout_to_select == "speaker":
            logger.info("Waiting for the speaker view button...")
            speaker_view_button = self.locate_element(step="speaker_view_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, "#custom-view-button-SpeakerViewButton, #SpeakerView-button")), wait_time_seconds=10)
            logger.info("Clicking the speaker view button...")
            self.click_element(speaker_view_button, "speaker_view_button")

        if layout_to_select == "gallery":
            logger.info("Waiting for the gallery view button...")
            gallery_view_button = self.locate_element(step="gallery_view_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, "#custom-view-button-MixedGridButton, #MixedGrid-button, #MixedGridView-button")), wait_time_seconds=10)
            logger.info("Clicking the gallery view button...")
            self.click_element(gallery_view_button, "gallery_view_button")

    def get_layout_to_select(self):
        if self.recording_view == RecordingViews.SPEAKER_VIEW:
            return "speaker"
        elif self.recording_view == RecordingViews.GALLERY_VIEW:
            return "gallery"
        elif self.recording_view == RecordingViews.SPEAKER_VIEW_NO_SIDEBAR:
            return "speaker"
        else:
            return "speaker"

    # Returns nothing if succeeded, raises an exception if failed
    def attempt_to_join_meeting(self):
        if self.teams_bot_login_credentials:
            self.login_to_microsoft_account()

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

        self.fill_out_name_input()

        self.turn_off_media_inputs()

        logger.info("Waiting for the Join now button...")
        join_button = self.locate_element(step="join_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, '[data-tid="prejoin-join-button"]')), wait_time_seconds=10)
        logger.info("Clicking the Join now button...")
        self.click_element(join_button, "join_button")

        # Wait for meeting to load and enable captions
        self.click_show_more_button()

        # Click the captions button
        self.click_captions_button()

        self.set_layout(self.get_layout_to_select())

        if self.disable_incoming_video:
            self.disable_incoming_video_in_ui()

        self.ready_to_show_bot_image()

    def disable_incoming_video_in_ui(self):
        logger.info("Waiting for the view button...")
        view_button = self.locate_element(step="view_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, "#view-mode-button, #custom-view-button")), wait_time_seconds=60)
        logger.info("Clicking the view button...")
        self.click_element(view_button, "disable_incoming_video:view_button")

        # Try to click the turn off incoming video button
        # If we can't find it, then look for the more options button and click it to reveal the turn off incoming video button
        num_attempts = 10
        logger.info("Waiting for the turn off incoming video button...")
        for attempt_index in range(num_attempts):
            try:
                turn_off_incoming_video_button = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, "[aria-label='Turn off incoming video'], #incoming-video-button")))
                logger.info("Turn off incoming video button found")
                turn_off_incoming_video_button.click()
                return
            except TimeoutException as e:
                more_options_button = self.find_element_by_selector(By.CSS_SELECTOR, "#ViewModeMoreOptionsMenuControl-id")
                if more_options_button:
                    logger.info("Clicking the more options button...")
                    self.click_element(more_options_button, "disable_incoming_video:more_options_button")

                last_check_timed_out = attempt_index == num_attempts - 1
                if last_check_timed_out:
                    logger.info("Could not find turn off incoming video button. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find turn off incoming video button. Timed out.", "disable_incoming_video:turn_off_incoming_video_button", e)
            except Exception as e:
                logger.info(f"Could not click turn off incoming video button. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException("Could not click turn off incoming video button. Unknown error.", "disable_incoming_video:turn_off_incoming_video_button", e)

    def click_leave_button(self):
        logger.info("Waiting for the leave button")
        leave_button = WebDriverWait(self.driver, 6).until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    '[data-inp="hangup-button"], #hangup-button',
                )
            )
        )

        logger.info("Clicking the leave button")
        leave_button.click()

    def click_cancel_join_button(self):
        logger.info("Waiting for the cancel button...")
        cancel_button = self.locate_element(step="cancel_button", condition=EC.presence_of_element_located((By.CSS_SELECTOR, '[data-tid="prejoin-cancel-button"]')), wait_time_seconds=10)
        logger.info("Clicking the cancel button...")
        self.click_element(cancel_button, "cancel_button")

    def login_to_microsoft_account(self):
        logger.info("Navigate to login screen")
        self.driver.get("https://www.office.com/login")

        logger.info("Waiting for the username input...")
        username_input = self.locate_element(step="username_input", condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="loginfmt"]')), wait_time_seconds=10)
        logger.info("Filling in the username...")
        username_input.send_keys(self.teams_bot_login_credentials["username"])

        time.sleep(1)

        logger.info("Looking for next button...")
        next_button = self.locate_element(step="next_button", condition=EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="submit"]')), wait_time_seconds=10)
        logger.info("Clicking the next button...")
        self.click_element(next_button, "next_button")

        time.sleep(1)

        logger.info("Waiting for the password input...")
        password_input = self.locate_element(step="password_input", condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="passwd"]')), wait_time_seconds=10)
        logger.info("Filling in the password...")
        password_input.send_keys(self.teams_bot_login_credentials["password"])

        time.sleep(1)

        logger.info("Looking for sign in button...")
        signin_button = self.locate_element(step="signin_button", condition=EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="submit"]')), wait_time_seconds=10)
        logger.info("Clicking the sign in button...")
        # Get the current page url
        url_before_signin = self.driver.current_url
        self.click_element(signin_button, "signin_button")

        logger.info("Login attempted, waiting for redirect...")
        ## Wait until the url changes to something other than the login page or too much time has passed
        start_waiting_at = time.time()
        while self.driver.current_url == url_before_signin:
            time.sleep(1)
            if time.time() - start_waiting_at > 60:
                logger.info("Login timed out, redirecting to meeting page")
                # TODO Replace with error message for login failed
                break

        logger.info(f"Redirected to {self.driver.current_url}")

        # If we see the incorrect password error, then we should raise an exception
        incorrect_password_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Your account or password is incorrect")]')
        if incorrect_password_element:
            logger.info("Incorrect password. Raising UiLoginAttemptFailedException")
            raise UiLoginAttemptFailedException("Incorrect password", "login_to_microsoft_account")

        logger.info("Login completed, redirecting to meeting page")
