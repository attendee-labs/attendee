import logging
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from bots.web_bot_adapter.ui_methods import UiAuthorizedUserNotInMeetingTimeoutExceededException, UiBlockedByCaptchaException, UiCouldNotJoinMeetingWaitingForHostException, UiCouldNotJoinMeetingWaitingRoomTimeoutException, UiCouldNotLocateElementException, UiIncorrectPasswordException, UiInfinitelyRetryableException

from .zoom_web_static_server import start_zoom_web_static_server

logger = logging.getLogger(__name__)


class UiZoomWebGenericJoinErrorException(UiInfinitelyRetryableException):
    def __init__(self, message, step=None, inner_exception=None):
        super().__init__(message, step, inner_exception)


class ZoomWebUIMethods:
    def __init__(self, driver):
        self.driver = driver

    def attempt_to_join_meeting(self):
        # Serve the HTML from a tiny local HTTP
        port = start_zoom_web_static_server()
        http_url = f"http://127.0.0.1:{port}/zoom_web_chromedriver_page.html"
        logger.info(f"Serving Zoom Web SDK HTML from {http_url}")

        self.driver.get(http_url)

        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": f"http://127.0.0.1:{port}",
                "permissions": ["geolocation", "audioCapture", "displayCapture", "videoCapture"],
            },
        )

        # Call the joinMeeting function
        self.driver.execute_script("joinMeeting()")

        self.wait_to_be_admitted_to_meeting()

        # Then find a button with the arial-label "More meeting control " and click it
        logger.info("Waiting for more meeting control button")
        more_meeting_control_button = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[aria-label='More meeting control ']")))
        logger.info("More meeting control button found, clicking")
        self.driver.execute_script("arguments[0].click();", more_meeting_control_button)

        # Then find an <a> tag with the arial label "Captions" and click it
        logger.info("Waiting for captions button")
        closed_captions_enabled = False
        try:
            captions_button = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[aria-label='Captions']")))
            logger.info("Captions button found, clicking")
            self.driver.execute_script("arguments[0].click();", captions_button)
            closed_captions_enabled = True
        except TimeoutException:
            logger.info("Captions button not found, so unable to transcribe via closed-captions.")
            self.could_not_enable_closed_captions()

        if not closed_captions_enabled:
            # If closed captions was not enabled, then click the more meeting control button again to close it
            # This resets the UI state
            logger.info("Closing the more meeting control button since closed captions was not enabled")
            more_meeting_control_button = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[aria-label='More meeting control ']")))
            logger.info("More meeting control button found, clicking")
            self.driver.execute_script("arguments[0].click();", more_meeting_control_button)

        if closed_captions_enabled:
            # Then find an <a> tag with the arial label "Your caption settings grouping Show Captions" and click it
            logger.info("Waiting for your caption settings grouping Show Captions button")
            your_caption_settings_grouping_show_captions_button = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[aria-label='Your caption settings grouping Show Captions']")))
            logger.info("Your caption settings grouping Show Captions button found, clicking")
            self.driver.execute_script("arguments[0].click();", your_caption_settings_grouping_show_captions_button)

            self.set_zoom_closed_captions_language()

        # Then see if it created a modal to select the caption language. If so, just click the save button
        try:
            logger.info("Waiting for save button")
            save_button = WebDriverWait(self.driver, 2).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'zm-btn--primary') and contains(text(), 'Save')]")))
            logger.info("Save button found, clicking")
            self.driver.execute_script("arguments[0].click();", save_button)
        except TimeoutException:
            # No modal appeared or Save button not found within 2 seconds, continue
            logger.info("No modal appeared or Save button not found within 2 seconds, continuing")

        # Then see if it created a modal to confirm that the meeting is being transcribed.
        try:
            logger.info("Waiting for OK button")
            ok_button = WebDriverWait(self.driver, 2).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'zm-btn--primary') and contains(text(), 'OK')]")))
            logger.info("OK button found, clicking")
            self.driver.execute_script("arguments[0].click();", ok_button)
        except TimeoutException:
            # No modal appeared or OK button not found within 2 seconds, continue
            logger.info("No modal appeared or OK button not found within 2 seconds, continuing")

        if self.disable_incoming_video:
            self.disable_incoming_video_in_ui()

        self.ready_to_show_bot_image()

    def click_leave_button(self):
        self.driver.execute_script("leaveMeeting()")

    def check_if_failed_to_join_because_onbehalf_token_user_not_in_meeting(self):
        failed_to_join_because_onbehalf_token_user_not_in_meeting = self.driver.execute_script("return window.userHasEncounteredOnBehalfTokenUserNotInMeetingError && window.userHasEncounteredOnBehalfTokenUserNotInMeetingError()")
        if failed_to_join_because_onbehalf_token_user_not_in_meeting:
            logger.warning("Bot failed to join because onbehalf token user not in meeting. Raising UiAuthorizedUserNotInMeetingTimeoutExceededException after sleeping for 5 seconds.")
            time.sleep(5)  # Sleep for 5 seconds, so we're not constantly retrying
            raise UiAuthorizedUserNotInMeetingTimeoutExceededException("Bot failed to join because onbehalf token user not in meeting")

    def check_if_failed_to_join_because_generic_join_error(self):
        failed_to_join_because_generic_join_error = self.driver.execute_script("return window.userHasEncounteredGenericJoinError && window.userHasEncounteredGenericJoinError()")
        if failed_to_join_because_generic_join_error:
            self.handle_generic_join_error()

    def wait_to_be_admitted_to_meeting(self):
        num_attempts_to_look_for_more_meeting_control_button = (self.automatic_leave_configuration.waiting_room_timeout_seconds + self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds) * 10
        logger.info("Waiting to be admitted to the meeting...")
        timeout_started_at = time.time()

        # We can either be waiting for the host to start meeting or we can be waiting to be admitted to the meeting
        is_waiting_for_host_to_start_meeting = False

        for attempt_index in range(num_attempts_to_look_for_more_meeting_control_button):
            try:
                # Query the userHasEnteredMeeting function (handle case where it's undefined)
                user_has_entered_meeting = self.driver.execute_script("return window.userHasEnteredMeeting && window.userHasEnteredMeeting()")
                if user_has_entered_meeting:
                    logger.info("We have been admitted to the meeting")
                    return
                time.sleep(1)
                raise TimeoutException("User has not entered the meeting")
            except TimeoutException as e:
                self.check_if_blocked_by_captcha()
                self.check_if_passcode_incorrect()
                self.check_if_failed_to_join_because_onbehalf_token_user_not_in_meeting()
                self.check_if_failed_to_join_because_generic_join_error()

                previous_is_waiting_for_host_to_start_meeting = is_waiting_for_host_to_start_meeting
                try:
                    is_waiting_for_host_to_start_meeting = self.driver.find_element(
                        By.XPATH,
                        '//*[contains(text(), "host to start the meeting")]',
                    ).is_displayed()
                except:
                    is_waiting_for_host_to_start_meeting = False

                # If we switch from waiting for the host to start the meeting to waiting to be admitted to the meeting, then we need to reset the timeout
                if previous_is_waiting_for_host_to_start_meeting != is_waiting_for_host_to_start_meeting:
                    logger.info(f"is_waiting_for_host_to_start_meeting changed from {previous_is_waiting_for_host_to_start_meeting} to {is_waiting_for_host_to_start_meeting}. Resetting timeout")
                    timeout_started_at = time.time()

                self.check_if_timeout_exceeded(timeout_started_at=timeout_started_at, step="wait_to_be_admitted_to_meeting", is_waiting_for_host_to_start_meeting=is_waiting_for_host_to_start_meeting)

                last_check_timed_out = attempt_index == num_attempts_to_look_for_more_meeting_control_button - 1
                if last_check_timed_out:
                    logger.info("Could not find more meeting control button. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException(
                        "Could not find more meeting control button. Timed out.",
                        "wait_to_be_admitted_to_meeting",
                        e,
                    )
            except Exception as e:
                logger.info(f"Could not find more meeting control button. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException(
                    "Could not find more meeting control button. Unknown error.",
                    "wait_to_be_admitted_to_meeting",
                    e,
                )

    def disable_incoming_video_in_ui(self):
        logger.info("Waiting for more meeting control button to disable incoming video")
        more_meeting_control_button = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[aria-label='More meeting control ']")))
        logger.info("More meeting control button found, clicking")
        self.driver.execute_script("arguments[0].click();", more_meeting_control_button)

        logger.info("Waiting for turn off incoming video button to disable incoming video")
        turn_off_incoming_video_button = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "[aria-label='Stop Incoming Video']")))
        logger.info("Turn off incoming video button found, clicking")
        self.driver.execute_script("arguments[0].click();", turn_off_incoming_video_button)

    def click_cancel_join_button(self):
        cancel_join_button = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "button.leave-btn")))
        logger.info("Cancel join button found, clicking")
        self.driver.execute_script("arguments[0].click();", cancel_join_button)

    def check_if_timeout_exceeded(self, timeout_started_at, step, is_waiting_for_host_to_start_meeting):
        if is_waiting_for_host_to_start_meeting:
            timeout_exceeded = time.time() - timeout_started_at > self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds
        else:
            timeout_exceeded = time.time() - timeout_started_at > self.automatic_leave_configuration.waiting_room_timeout_seconds

        if timeout_exceeded:
            # If there is more than one participant in the meeting, then the bot was just let in and we should not timeout
            if len(self.participants_info) > 1:
                logger.info(f"Timeout exceeded, but there is more than one participant in the meeting. Not aborting join attempt. is_waiting_for_host_to_start_meeting={is_waiting_for_host_to_start_meeting}")
                return

            try:
                self.click_cancel_join_button()
            except Exception:
                logger.info("Error clicking cancel join button, but not a fatal error")

            self.abort_join_attempt()

            if is_waiting_for_host_to_start_meeting:
                logger.info("Waiting for host to start meeting timeout exceeded. Raising UiCouldNotJoinMeetingWaitingForHostToStartMeetingException")
                raise UiCouldNotJoinMeetingWaitingForHostException("Waiting for host to start meeting timeout exceeded", step)
            else:
                logger.info("Waiting room timeout exceeded. Raising UiCouldNotJoinMeetingWaitingRoomTimeoutException")
                raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", step)

    def check_if_passcode_incorrect(self):
        passcode_incorrect_element = None
        try:
            passcode_incorrect_element = self.driver.find_element(
                By.XPATH,
                '//*[contains(text(), "Passcode wrong")]',
            )
        except:
            return

        if passcode_incorrect_element and passcode_incorrect_element.is_displayed():
            logger.info("Passcode incorrect. Raising UiIncorrectPasswordException")
            raise UiIncorrectPasswordException("Passcode incorrect")

    def check_if_blocked_by_captcha(self):
        """
        Detects the Zoom Web SDK captcha/verification challenge UI.

        Some Zoom accounts may be forced through a "Check Captcha" flow which can reappear
        after submitting the verification code, effectively blocking programmatic joining.
        See: https://devforum.zoom.us/t/check-captcha-button-show-again-after-filling-in-the-verification-code/25076
        """
        upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        lower = "abcdefghijklmnopqrstuvwxyz"
        xpath = f"//button[contains(translate(normalize-space(.), '{upper}', '{lower}'), 'check captcha')]"

        try:
            candidates = self.driver.find_elements(By.XPATH, xpath) or []
        except Exception:
            return

        for el in candidates:
            try:
                if el and el.is_displayed():
                    logger.info("Blocked by captcha / verification challenge detected (button text). Raising UiBlockedByCaptchaException")
                    raise UiBlockedByCaptchaException("Blocked by captcha (Zoom Web SDK verification challenge)")
            except UiBlockedByCaptchaException:
                raise
            except Exception:
                # If the element becomes stale between queries, ignore and continue scanning.
                continue

    def set_zoom_closed_captions_language(self):
        if not self.zoom_closed_captions_language:
            return

        logger.info(f"Setting closed captions language to {self.zoom_closed_captions_language}")

        # Find the transcription language input element
        try:
            logger.info("Waiting for transcription language input")
            language_input = None
            try:
                language_input = WebDriverWait(self.driver, 2).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.transcription-language__input")))
            except TimeoutException:
                logger.warning("Could not find transcription language input element")

            if not language_input:
                language_input = self.retrieve_language_input_from_bottom_panel()
            logger.info("Transcription language input found, focusing and typing language")

            # Focus on the input element and type the language
            language_input.click()
            language_input.clear()  # Clear any existing text
            language_input.send_keys(self.zoom_closed_captions_language)
            language_input.send_keys(Keys.RETURN)  # Press Enter

            logger.info(f"Successfully set closed captions language to {self.zoom_closed_captions_language}")
        except TimeoutException:
            logger.warning("Could not find transcription language input element")
        except Exception as e:
            logger.warning(f"Error setting transcription language: {e}")

    def retrieve_language_input_from_bottom_panel(self):
        # Then find a button with the arial-label "More meeting control " and click it
        logger.info("Waiting for more meeting control button")
        more_meeting_control_button = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[aria-label='More meeting control ']")))
        logger.info("More meeting control button found, clicking")
        self.driver.execute_script("arguments[0].click();", more_meeting_control_button)

        # Then find an <a> tag with the arial label "Captions" and click it
        logger.info("Waiting for captions button")
        captions_button = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[aria-label='Captions']")))
        logger.info("Captions button found, clicking")
        self.driver.execute_script("arguments[0].click();", captions_button)

        # Then find an <a> tag with the arial label "Your caption settings grouping Show Captions" and click it
        logger.info("Waiting for your caption settings grouping Host controls grouping My Caption Language")
        host_controls_grouping_my_caption_language_button = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[aria-label='Host controls grouping My Caption Language']")))
        logger.info("Host controls grouping My Caption Language button found, clicking")
        self.driver.execute_script("arguments[0].click();", host_controls_grouping_my_caption_language_button)

        # Find the first unchecked element in the transcription list and click it
        logger.info("Waiting for first unchecked transcription option")
        first_unchecked_option = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.XPATH, "//*[contains(@class, 'transcription-list')]//*[@aria-checked='false'][1]")))
        logger.info("First unchecked transcription option found, clicking")
        self.driver.execute_script("arguments[0].click();", first_unchecked_option)

        language_input = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.transcription-language__input")))

        return language_input
