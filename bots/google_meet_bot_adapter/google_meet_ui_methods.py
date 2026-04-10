import logging
import os
import random
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from django.conf import settings

from bots.bot_sso_utils import get_google_meet_set_cookie_url
from bots.models import RecordingViews
from bots.web_bot_adapter.ui_methods import (
    UiCouldNotClickElementException,
    UiCouldNotJoinMeetingWaitingForHostException,
    UiCouldNotJoinMeetingWaitingRoomTimeoutException,
    UiCouldNotLocateElementException,
    UiLoginAttemptFailedException,
    UiLoginRequiredException,
    UiMeetingNotFoundException,
    UiRequestToJoinDeniedException,
    UiRetryableExpectedException,
)

logger = logging.getLogger(__name__)


class By:
    CSS_SELECTOR = "css"
    XPATH = "xpath"


class TimeoutException(Exception):
    pass


class NoSuchElementException(Exception):
    pass


class ElementNotInteractableException(Exception):
    pass


@dataclass(frozen=True)
class CdpElement:
    selector_type: str
    selector: str
    index: int | None = None


class UiGoogleBlockingUsException(UiRetryableExpectedException):
    def __init__(self, message, step=None, inner_exception=None):
        super().__init__(message, step, inner_exception)


class GoogleMeetUIMethods:
    def _navigate(self, url: str) -> None:
        self.driver.navigate(url)

    def _current_url(self) -> str:
        return self.driver.execute_script("return window.location.href;") or ""

    def _get_cookies(self) -> list[dict]:
        try:
            result = self.driver.execute_cdp_cmd("Network.getCookies", {})
            return result.get("cookies", [])
        except Exception:
            return []

    def _delete_all_cookies(self) -> None:
        self.driver.execute_cdp_cmd("Network.clearBrowserCookies", {})

    def _query_element_info(self, selector_type: str, selector: str, index: int | None = None):
        return self.driver.execute_script(
            """
            const [selectorType, selector, index] = arguments;

            function findElement() {
              if (selectorType === 'css') {
                const els = Array.from(document.querySelectorAll(selector));
                if (index == null) return els[0] || null;
                return els[index] || null;
              }

              if (selectorType === 'xpath') {
                const result = document.evaluate(
                  selector,
                  document,
                  null,
                  XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                  null,
                );
                const idx = index == null ? 0 : index;
                if (result.snapshotLength <= idx) return null;
                return result.snapshotItem(idx);
              }

              throw new Error(`Unsupported selector type: ${selectorType}`);
            }

            const el = findElement();
            if (!el) return null;

            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const visible = (
              !!style &&
              style.display !== 'none' &&
              style.visibility !== 'hidden' &&
              style.opacity !== '0' &&
              rect.width > 0 &&
              rect.height > 0
            );

            return {
              text: (el.innerText || el.textContent || '').trim(),
              html: el.outerHTML,
              value: ('value' in el) ? el.value : null,
              visible,
              disabled: !!el.disabled,
              checked: !!el.checked,
              rect: {
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
                top: rect.top,
                left: rect.left,
                right: rect.right,
                bottom: rect.bottom,
              },
            };
            """,
            selector_type,
            selector,
            index,
        )

    def _element_exists(self, selector_type: str, selector: str, index: int | None = None) -> bool:
        return self._query_element_info(selector_type, selector, index) is not None

    def _wait_for_element(
        self,
        *,
        step: str,
        selector_type: str,
        selector: str,
        wait_time_seconds: float = 60,
        visible_only: bool = False,
        index: int | None = None,
    ) -> CdpElement:
        deadline = time.time() + wait_time_seconds
        last_info = None
        while time.time() < deadline:
            info = self._query_element_info(selector_type, selector, index)
            if info is not None:
                last_info = info
                if not visible_only or info.get("visible"):
                    return CdpElement(selector_type=selector_type, selector=selector, index=index)
            time.sleep(0.1)

        if last_info is None:
            raise TimeoutException(f"Timed out waiting for {selector_type} selector {selector}")
        raise ElementNotInteractableException(f"Element found but not interactable for step {step}")

    def _wait_for_element_to_disappear(self, selector_type: str, selector: str, wait_time_seconds: float) -> bool:
        deadline = time.time() + wait_time_seconds
        while time.time() < deadline:
            if not self._element_exists(selector_type, selector):
                return True
            time.sleep(0.1)
        return False

    def _scroll_element_into_view(self, element: CdpElement) -> None:
        result = self.driver.execute_script(
            """
            const [selectorType, selector, index] = arguments;

            function findElement() {
              if (selectorType === 'css') {
                const els = Array.from(document.querySelectorAll(selector));
                if (index == null) return els[0] || null;
                return els[index] || null;
              }

              const result = document.evaluate(
                selector,
                document,
                null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                null,
              );
              const idx = index == null ? 0 : index;
              if (result.snapshotLength <= idx) return null;
              return result.snapshotItem(idx);
            }

            const el = findElement();
            if (!el) return false;
            el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
            return true;
            """,
            element.selector_type,
            element.selector,
            element.index,
        )
        if not result:
            raise NoSuchElementException(f"Element disappeared before scroll: {element}")

    def _focus_element(self, element: CdpElement) -> None:
        result = self.driver.execute_script(
            """
            const [selectorType, selector, index] = arguments;

            function findElement() {
              if (selectorType === 'css') {
                const els = Array.from(document.querySelectorAll(selector));
                if (index == null) return els[0] || null;
                return els[index] || null;
              }

              const result = document.evaluate(
                selector,
                document,
                null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                null,
              );
              const idx = index == null ? 0 : index;
              if (result.snapshotLength <= idx) return null;
              return result.snapshotItem(idx);
            }

            const el = findElement();
            if (!el) return false;
            el.focus();
            return true;
            """,
            element.selector_type,
            element.selector,
            element.index,
        )
        if not result:
            raise NoSuchElementException(f"Element disappeared before focus: {element}")

    def _dispatch_mouse_move(self, x: float, y: float) -> None:
        self.driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": float(x),
                "y": float(y),
                "button": "none",
                "buttons": 0,
            },
        )

    def _dispatch_mouse_click(self, x: float, y: float) -> None:
        self.driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        self.driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )

    def _dispatch_key(self, key: str, code: str | None = None, windows_virtual_key_code: int | None = None) -> None:
        if code is None:
            code = key
        payload = {
            "type": "rawKeyDown",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": windows_virtual_key_code or (8 if key == "Backspace" else 0),
            "nativeVirtualKeyCode": windows_virtual_key_code or (8 if key == "Backspace" else 0),
        }
        self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", payload)
        payload["type"] = "keyUp"
        self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", payload)

    def locate_element(self, step, selector_type, selector, wait_time_seconds=60, visible_only=False, index=None):
        try:
            return self._wait_for_element(
                step=step,
                selector_type=selector_type,
                selector=selector,
                wait_time_seconds=wait_time_seconds,
                visible_only=visible_only,
                index=index,
            )
        except Exception as e:
            logger.warning(f"Exception raised in locate_element for {step}")
            raise UiCouldNotLocateElementException(f"Exception raised in locate_element for {step}", step, e)

    def find_element_by_selector(self, selector_type, selector):
        try:
            if self._element_exists(selector_type, selector):
                return CdpElement(selector_type=selector_type, selector=selector)
            return None
        except Exception as e:
            logger.warning(f"Unknown error occurred in find_element_by_selector. Exception type = {type(e)}")
            return None

    def click_element_and_handle_blocking_elements(self, element, step):
        num_attempts = 30

        for attempt_index in range(num_attempts):
            try:
                self.click_element(element, step)
                return
            except UiCouldNotClickElementException as e:
                logger.warning(f"Error occurred when clicking element for step {step}, will click any blocking elements and retry the click")
                self.click_others_may_see_your_meeting_differently_button(step)
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e

    def click_element_forcefully(self, element, step):
        try:
            result = self.driver.execute_script(
                """
                const [selectorType, selector, index] = arguments;

                function findElement() {
                  if (selectorType === 'css') {
                    const els = Array.from(document.querySelectorAll(selector));
                    if (index == null) return els[0] || null;
                    return els[index] || null;
                  }

                  const result = document.evaluate(
                    selector,
                    document,
                    null,
                    XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                    null,
                  );
                  const idx = index == null ? 0 : index;
                  if (result.snapshotLength <= idx) return null;
                  return result.snapshotItem(idx);
                }

                const el = findElement();
                if (!el) return false;
                el.click();
                return true;
                """,
                element.selector_type,
                element.selector,
                element.index,
            )
            if not result:
                raise NoSuchElementException(f"Element disappeared before force click: {element}")
        except Exception as e:
            logger.warning(f"Error occurred when forcefully clicking element for step {step}, will retry")
            raise UiCouldNotClickElementException("Error occurred when forcefully clicking element", step, e)

    def click_element(self, element, step):
        try:
            self._scroll_element_into_view(element)
            info = self._query_element_info(element.selector_type, element.selector, element.index)
            if not info or not info.get("visible"):
                raise ElementNotInteractableException(f"Element is not visible for step {step}")

            rect = info["rect"]
            click_x = rect["x"] + rect["width"] / 2
            click_y = rect["y"] + rect["height"] / 2
            self._dispatch_mouse_move(click_x, click_y)
            time.sleep(random.uniform(0.02, 0.08))
            self._dispatch_mouse_click(click_x, click_y)
        except Exception as e:
            logger.warning(f"Error occurred when clicking element for step {step}, will retry. Exception class name was {e.__class__.__name__}")
            raise UiCouldNotClickElementException("Error occurred when clicking element", step, e)

    def click_this_meeting_is_being_recorded_join_now_button(self, step):
        this_meeting_is_being_recorded_join_now_button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Join now"]]')
        if this_meeting_is_being_recorded_join_now_button:
            logger.info("Clicking this_meeting_is_being_recorded_join_now_button")
            self.click_element(this_meeting_is_being_recorded_join_now_button, step)

    def click_others_may_see_your_meeting_differently_button(self, step):
        others_may_see_your_meeting_differently_button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Got it"]]')
        if others_may_see_your_meeting_differently_button:
            logger.info("Clicking others_may_see_your_meeting_differently_button")
            self.click_element_forcefully(others_may_see_your_meeting_differently_button, step)

    def look_for_blocked_element(self, step):
        cannot_join_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "You can\'t join this video call") or contains(text(), "There is a problem connecting to this video call")]')
        if cannot_join_element:
            element_text = self._query_element_info(cannot_join_element.selector_type, cannot_join_element.selector, cannot_join_element.index).get("text", "")
            logger.warning(f"Google is blocking us for whatever reason, but we can retry. Element text: '{element_text}'. Raising UiGoogleBlockingUsException")
            raise UiGoogleBlockingUsException("You can't join this video call", step)

    def look_for_login_required_element(self, step):
        login_required_element = self.find_element_by_selector(By.XPATH, '//h1[contains(., "Sign in")]/parent::*[.//*[contains(text(), "your Google Account")]]')
        if login_required_element:
            logger.warning("Login required. Raising UiLoginRequiredException")
            raise UiLoginRequiredException("Login required", step)

    def look_for_denied_your_request_element(self, step):
        denied_your_request_element = self.find_element_by_selector(
            By.XPATH,
            '//*[contains(text(), "Someone in the call denied your request to join") or contains(text(), "No one responded to your request to join the call") or contains(text(), "You left the meeting")]',
        )
        if not denied_your_request_element:
            return

        element_text = self._query_element_info(denied_your_request_element.selector_type, denied_your_request_element.selector).get("text", "")

        if "Someone in the call denied your request to join" in element_text:
            logger.warning("Someone in the call actively denied our request to join. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("Someone in the call denied your request to join", step)
        elif "No one responded to your request to join the call" in element_text:
            logger.warning("No one responded to our request to join (timeout). Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("No one responded to your request to join the call", step)
        else:
            logger.warning("Saw 'You left the meeting' element. Happens if someone actively denied our request to join. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("You left the meeting", step)

    def look_for_asking_to_be_let_in_element_after_waiting_period_expired(self, step):
        asking_to_be_let_in_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Asking to be let in")]')
        if asking_to_be_let_in_element:
            logger.warning("Bot was not let in after waiting period expired. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("Bot was not let in after waiting period expired", step)

    def check_if_waiting_room_timeout_exceeded(self, waiting_room_timeout_started_at, step):
        waiting_room_timeout_exceeded = time.time() - waiting_room_timeout_started_at > self.automatic_leave_configuration.waiting_room_timeout_seconds
        if waiting_room_timeout_exceeded:
            if len(self.participants_info) > 1:
                logger.warning("Waiting room timeout exceeded, but there is more than one participant in the meeting. Not aborting join attempt.")
                return
            self.abort_join_attempt()
            logger.warning("Waiting room timeout exceeded. Raising UiCouldNotJoinMeetingWaitingRoomTimeoutException")
            raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", step)

    def turn_off_media_inputs(self):
        logger.info("Waiting for the microphone button...")
        microphone_button = self.locate_element(
            step="turn_off_microphone_button",
            selector_type=By.CSS_SELECTOR,
            selector='div[aria-label="Turn off microphone"], button[aria-label="Turn off microphone"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        self.bezier_mouse_move_to_target_element(microphone_button)
        time.sleep(random.uniform(0.1, 0.3))
        logger.info("Clicking the microphone button...")
        self.click_element(microphone_button, "turn_off_microphone_button")

        time.sleep(random.uniform(0.2, 0.5))

        logger.info("Waiting for the camera button...")
        camera_button = self.locate_element(
            step="turn_off_camera_button",
            selector_type=By.CSS_SELECTOR,
            selector='div[aria-label="Turn off camera"], button[aria-label="Turn off camera"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        self.bezier_mouse_move_to_target_element(camera_button)
        time.sleep(random.uniform(0.1, 0.3))
        logger.info("Clicking the camera button...")
        self.click_element(camera_button, "turn_off_camera_button")

    def join_now_button_selector(self):
        return '//button[.//span[text()="Ask to join" or text()="Join now" or text()="Join the call now"]]'

    def check_for_failed_logged_in_bot_attempt(self):
        if not self.google_meet_bot_login_session:
            return
        logger.warning("Bot attempted to login, but name input is present, so the bot was not logged in. Raising UiLoginAttemptFailedException")
        raise UiLoginAttemptFailedException("Bot attempted to login, but name input is present, so the bot was not logged in.", "name_input")

    def join_now_button_is_present(self):
        join_button = self.find_element_by_selector(By.XPATH, self.join_now_button_selector())
        return bool(join_button)

    def retrieve_name_input_element(self):
        return self._wait_for_element(
            step="name_input",
            selector_type=By.CSS_SELECTOR,
            selector='input[type="text"][aria-label="Your name"]',
            wait_time_seconds=1,
            visible_only=True,
        )

    def human_type_with_typos(self, element, text, typo_rate=0.03):
        self._scroll_element_into_view(element)
        self._focus_element(element)
        self.driver.execute_script(
            """
            const [selectorType, selector, index] = arguments;
            function findElement() {
              if (selectorType === 'css') {
                const els = Array.from(document.querySelectorAll(selector));
                if (index == null) return els[0] || null;
                return els[index] || null;
              }
              const result = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
              const idx = index == null ? 0 : index;
              if (result.snapshotLength <= idx) return null;
              return result.snapshotItem(idx);
            }
            const el = findElement();
            if (!el) return false;
            if ('value' in el) el.value = '';
            el.dispatchEvent(new Event('input', { bubbles: true }));
            return true;
            """,
            element.selector_type,
            element.selector,
            element.index,
        )
        for char in text:
            if random.random() < typo_rate:
                wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                self.driver.execute_cdp_cmd("Input.insertText", {"text": wrong})
                time.sleep(random.uniform(0.1, 0.3))
                self._dispatch_key("Backspace", code="Backspace", windows_virtual_key_code=8)
                time.sleep(random.uniform(0.05, 0.2))
            self.driver.execute_cdp_cmd("Input.insertText", {"text": char})
            time.sleep(random.uniform(0.04, 0.18))

    def bezier_mouse_move_to_target_element(self, target_element, num_points=20):
        try:
            self._scroll_element_into_view(target_element)
            info = self._query_element_info(target_element.selector_type, target_element.selector, target_element.index)
            if not info:
                raise NoSuchElementException(f"Target element disappeared: {target_element}")

            rect = info["rect"]
            end_x = rect["x"] + rect["width"] / 2
            end_y = rect["y"] + rect["height"] / 2

            viewport = self.driver.execute_script("return { width: window.innerWidth, height: window.innerHeight };")
            viewport_w = viewport["width"]
            viewport_h = viewport["height"]

            start_x = random.uniform(viewport_w * 0.1, viewport_w * 0.9)
            start_y = random.uniform(viewport_h * 0.1, viewport_h * 0.9)

            mid_x, mid_y = (start_x + end_x) / 2, (start_y + end_y) / 2
            ctrl_x = max(10, min(viewport_w - 10, mid_x + random.randint(-100, 100)))
            ctrl_y = max(10, min(viewport_h - 10, mid_y + random.randint(-100, 100)))

            for i in range(num_points + 1):
                t = i / num_points
                x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t**2 * end_x
                y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t**2 * end_y
                x = max(1, min(viewport_w - 1, x))
                y = max(1, min(viewport_h - 1, y))
                self._dispatch_mouse_move(x, y)
                time.sleep(random.uniform(0.005, 0.03))
        except Exception as e:
            logger.warning(f"Bézier mouse move failed ({type(e).__name__}: {e}), falling back to direct move")
            info = self._query_element_info(target_element.selector_type, target_element.selector, target_element.index)
            if info:
                rect = info["rect"]
                self._dispatch_mouse_move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)

    def fill_out_name_input(self):
        num_attempts_to_look_for_name_input = 300
        logger.info("Waiting for the name input field...")
        for attempt_to_look_for_name_input_index in range(num_attempts_to_look_for_name_input):
            try:
                name_input = self.retrieve_name_input_element()
                time.sleep(0.5)
                self.check_for_failed_logged_in_bot_attempt()
                logger.info("name input found")
                self.human_type_with_typos(name_input, self.display_name)
                return
            except TimeoutException as e:
                self.look_for_blocked_element("name_input")
                self.look_for_login_required_element("name_input")

                if self.google_meet_bot_login_session and self.join_now_button_is_present():
                    logger.info("This is a signed in bot and name input is not present but the join now button is present. Assuming name input is not present because we don't need to fill it out, so returning.")
                    return

                last_check_timed_out = attempt_to_look_for_name_input_index == num_attempts_to_look_for_name_input - 1
                if last_check_timed_out:
                    logger.warning("Could not find name input. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find name input. Timed out.", "name_input", e)

            except ElementNotInteractableException as e:
                logger.warning("Name input is not interactable. Going to try again.")
                last_check_non_interactable = attempt_to_look_for_name_input_index == num_attempts_to_look_for_name_input - 1
                if last_check_non_interactable:
                    logger.warning("Could not find name input. Non interactable. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find name input. Non interactable.", "name_input", e)

            except UiLoginAttemptFailedException as e:
                raise e

            except Exception as e:
                logger.warning(f"Could not find name input. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException("Could not find name input. Unknown error.", "name_input", e)

    def click_captions_button(self):
        num_attempts_to_look_for_captions_button = 600
        logger.info("Waiting for captions button...")
        waiting_room_timeout_started_at = time.time()
        for attempt_to_look_for_captions_button_index in range(num_attempts_to_look_for_captions_button):
            try:
                captions_button = self.locate_element(
                    step="click_captions_button",
                    selector_type=By.CSS_SELECTOR,
                    selector='button[aria-label="Turn on captions"]',
                    wait_time_seconds=1,
                    visible_only=True,
                )
                logger.info("Captions button found")
                self.click_element(captions_button, "click_captions_button")
                logger.info("Waiting for captions to be enabled...")
                self.locate_element(
                    step="captions_enabled_button",
                    selector_type=By.CSS_SELECTOR,
                    selector='button[aria-label="Turn off captions"]',
                    wait_time_seconds=5,
                    visible_only=True,
                )
                logger.info("Confirmed captions were enabled")
                return
            except UiCouldNotClickElementException as e:
                self.click_this_meeting_is_being_recorded_join_now_button("click_captions_button")
                self.click_others_may_see_your_meeting_differently_button("click_captions_button")
                last_check_could_not_click_element = attempt_to_look_for_captions_button_index == num_attempts_to_look_for_captions_button - 1
                if last_check_could_not_click_element:
                    logger.warning("Could not click captions button. Raising UiCouldNotClickElementException")
                    raise e
            except UiCouldNotLocateElementException as e:
                self.look_for_blocked_element("click_captions_button")
                self.look_for_denied_your_request_element("click_captions_button")
                self.click_this_meeting_is_being_recorded_join_now_button("click_captions_button")
                self.click_others_may_see_your_meeting_differently_button("click_captions_button")
                self.check_if_waiting_room_timeout_exceeded(waiting_room_timeout_started_at, "click_captions_button")

                last_check_timed_out = attempt_to_look_for_captions_button_index == num_attempts_to_look_for_captions_button - 1
                if last_check_timed_out:
                    self.look_for_asking_to_be_let_in_element_after_waiting_period_expired("click_captions_button")
                    logger.warning("Could not find captions button. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException(
                        "Could not find captions button. Timed out.",
                        "click_captions_button",
                        e,
                    )
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Could not find captions button. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException(
                    "Could not find captions button. Unknown error.",
                    "click_captions_button",
                    e,
                )

    def check_if_meeting_is_found(self):
        meeting_not_found_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Check your meeting code") or contains(text(), "Invalid video call name") or contains(text(), "Your meeting code has expired")]')
        if meeting_not_found_element:
            logger.warning("Meeting not found. Raising UiMeetingNotFoundException")
            raise UiMeetingNotFoundException("Meeting not found", "check_if_meeting_is_found")

    def wait_for_host_if_needed(self):
        host_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Waiting for the host to join")]')
        if host_element:
            wait_time_seconds = self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds
            logger.info(f"We must wait for the host to join before we can join the meeting. Waiting for {wait_time_seconds} seconds...")
            disappeared = self._wait_for_element_to_disappear(By.XPATH, '//*[contains(text(), "Waiting for the host to join")]', wait_time_seconds)
            if not disappeared:
                logger.warning("Host did not join the meeting in time. Raising UiCouldNotJoinMeetingWaitingForHostException")
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
            logger.warning(f"Error turning off reactions: {e}")

    def attempt_to_turn_off_reactions(self):
        logger.info("Attempting to turn off reactions")
        logger.info("Waiting for the more options button...")
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            selector_type=By.CSS_SELECTOR,
            selector='button[jsname="NakZHc"][aria-label="More options"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            selector_type=By.XPATH,
            selector='//li[.//span[text()="Settings"]]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "settings_list_item")

        logger.info("Waiting for the reactions tab...")
        self.locate_element(
            step="reactions_tab",
            selector_type=By.CSS_SELECTOR,
            selector='button[aria-label="Reactions"]',
            wait_time_seconds=6,
            visible_only=True,
        )

        toggle_result = self.driver.execute_script(
            """
            const button = document.querySelector('button[aria-label="Show reactions from others"]');
            if (!button) return false;
            button.click();
            return true;
            """
        )
        if not toggle_result:
            raise UiCouldNotLocateElementException("Could not find reactions toggle", "reactions_toggle")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button_for_language_selection",
            selector_type=By.CSS_SELECTOR,
            selector='button[aria-label="Close dialog"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def disable_incoming_video_in_ui(self):
        logger.info("Disabling incoming video")
        logger.info("Waiting for the more options button...")
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            selector_type=By.CSS_SELECTOR,
            selector='button[jsname="NakZHc"][aria-label="More options"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "disable_incoming_video:more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            selector_type=By.XPATH,
            selector='//li[.//span[text()="Settings"]]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "disable_incoming_video:settings_list_item")

        logger.info("Waiting for the video button...")
        video_button = self.locate_element(
            step="video_button",
            selector_type=By.CSS_SELECTOR,
            selector='button[aria-label="Video"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the video button...")
        self.click_element(video_button, "disable_incoming_video:video_button")

        logger.info("Waiting for the Audio only option...")
        audio_only_option = self.locate_element(
            step="audio_only_option",
            selector_type=By.CSS_SELECTOR,
            selector='li[aria-label="Audio only"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the Audio only option...")
        self.click_element_forcefully(audio_only_option, "disable_incoming_video:audio_only_option")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button",
            selector_type=By.CSS_SELECTOR,
            selector='[aria-modal="true"] button[aria-label="Close dialog"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "disable_incoming_video:close_button")

        logger.info("Incoming video disabled")

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
                logger.warning(f"Error setting layout: {e}. Retrying. Attempt #{attempt_index}...")

    def attempt_to_set_layout(self, layout_to_select):
        logger.info("Begin setting layout. Waiting for the more options button...")
        more_options_button = self.locate_element(
            step="more_options_button",
            selector_type=By.CSS_SELECTOR,
            selector='button[jsname="NakZHc"][aria-label="More options"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the more options button....")
        self.click_element_and_handle_blocking_elements(more_options_button, "more_options_button")

        logger.info("Waiting for the 'Change layout' list item...")
        change_layout_list_item = self.locate_element(
            step="change_layout_item",
            selector_type=By.XPATH,
            selector='//li[.//span[text()="Change layout" or text()="Adjust view"] or @jsname="WZerud"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the 'Change layout' list item....")
        self.click_element_and_handle_blocking_elements(change_layout_list_item, "change_layout_list_item")

        if layout_to_select == "spotlight":
            logger.info("Waiting for the 'Spotlight' label element")
            spotlight_label = self.locate_element(
                step="spotlight_label",
                selector_type=By.XPATH,
                selector='//label[.//span[text()="Spotlight"]]',
                wait_time_seconds=6,
                visible_only=True,
            )
            logger.info("Clicking the 'Spotlight' label element")
            self.click_element(spotlight_label, "spotlight_label")

        if layout_to_select == "sidebar":
            logger.info("Waiting for the 'Sidebar' label element")
            sidebar_label = self.locate_element(
                step="sidebar_label",
                selector_type=By.XPATH,
                selector='//label[.//span[text()="Sidebar"]]',
                wait_time_seconds=6,
                visible_only=True,
            )
            logger.info("Clicking the 'Sidebar' label element")
            self.click_element(sidebar_label, "sidebar_label")

        if layout_to_select == "tiled":
            logger.info("Waiting for the 'Tiled' label element")
            tiled_label = self.locate_element(
                step="tiled_label",
                selector_type=By.XPATH,
                selector='//label[.//span[@class="xo15nd" and contains(text(), "Tiled")]]',
                wait_time_seconds=6,
                visible_only=True,
            )
            logger.info("Clicking the 'Tiled' label element")
            self.click_element(tiled_label, "tiled_label")

            logger.info("Waiting for the tile selector element")
            self.locate_element(
                step="tile_selector",
                selector_type=By.CSS_SELECTOR,
                selector='.ByPkaf',
                wait_time_seconds=6,
                visible_only=True,
            )

            logger.info("Clicking the last tile option (49 tiles)")
            clicked = self.driver.execute_script(
                """
                const options = Array.from(document.querySelectorAll('.ByPkaf .gyG0mb-zD2WHb-SYOSDb-OWXEXe-mt1Mkb'));
                if (!options.length) return false;
                options[options.length - 1].click();
                return true;
                """
            )
            if not clicked:
                logger.warning("No tile options found")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button",
            selector_type=By.CSS_SELECTOR,
            selector='[aria-modal="true"] button[aria-label="Close"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def wait_until_url_has_stopped_changing(self, stable_for: float = 1.0, timeout: float = 30.0, poll: float = 0.1) -> bool:
        last_url = self._current_url()
        last_change = time.monotonic()
        deadline = last_change + timeout

        while time.monotonic() < deadline:
            current_url = self._current_url()
            if current_url != last_url:
                last_url = current_url
                last_change = time.monotonic()

            if (time.monotonic() - last_change) >= stable_for:
                logger.info("URL has not changed for %.2f seconds, returning (url=%s)", stable_for, current_url)
                return True

            time.sleep(poll)

        logger.info("Timed out waiting for URL stability (>%.2fs). Last URL: %s", stable_for, last_url)
        return False

    def login_to_google_meet_account_with_retries(self):
        num_attempts = 3
        for attempt_index in range(num_attempts):
            try:
                self.login_to_google_meet_account()
                return
            except UiLoginAttemptFailedException as e:
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e
                logger.warning(f"Error logging in to Google Meet account. Clearing cookies and retrying... Attempts remaining: {num_attempts - attempt_index - 1}")
                self._delete_all_cookies()

    def safely_navigate_to_gmail_domain_url(self):
        gmail_service_url = f"https://www.google.com/a/{self.google_meet_bot_login_session.get('login_domain')}/ServiceLogin?service=mail"
        logger.info(f"Making request to gmail service url: {gmail_service_url}")
        response = requests.get(gmail_service_url, allow_redirects=False)
        redirect_url_from_google = response.headers.get("Location")

        redirect_url_from_google_host = None
        try:
            redirect_url_from_google_host = urlparse(redirect_url_from_google).hostname
        except Exception:
            pass

        if redirect_url_from_google_host != settings.SITE_DOMAIN:
            logger.error(f"Redirect url's host is not SITE_DOMAIN. Redirect url: {redirect_url_from_google}. Redirect url's host: {redirect_url_from_google_host}. SITE_DOMAIN: {settings.SITE_DOMAIN}")
            raise UiLoginAttemptFailedException("Redirect url's host is not SITE_DOMAIN", "safe_navigate_to_gmail_domain_url")

        logger.info(f"redirect_url_from_google_host = {redirect_url_from_google_host}")
        self._navigate(redirect_url_from_google)

    def navigate_to_gmail_domain_url(self):
        if os.getenv("USE_SAFE_NAVIGATION_FOR_SIGNED_IN_GOOGLE_MEET_BOTS", "false") == "true":
            self.safely_navigate_to_gmail_domain_url()
            return

        gmail_domain_url = f"https://mail.google.com/a/{self.google_meet_bot_login_session.get('login_domain')}"
        logger.info(f"Navigating to gmail domain url: {gmail_domain_url}")
        self._navigate(gmail_domain_url)

    def login_to_google_meet_account(self):
        self.google_meet_bot_login_session = self.create_google_meet_bot_login_session_callback()
        logger.info("Logging in to Google Meet account")
        session_id = self.google_meet_bot_login_session.get("session_id")
        google_meet_set_cookie_url = get_google_meet_set_cookie_url(session_id)
        logger.info(f"Navigating to Google Meet set cookie URL: {google_meet_set_cookie_url}")
        self._navigate(google_meet_set_cookie_url)

        self.navigate_to_gmail_domain_url()

        start_waiting_at = time.time()
        while not self.has_google_cookies_that_indicate_logged_in(self.driver):
            time.sleep(1)
            logger.info(f"Waiting for cookies indicating that we have logged in successfully. Current URL: {self._current_url()}")
            if time.time() - start_waiting_at > 30:
                logger.warning(f"Login timed out, after 30 seconds, no Google auth cookies were present. Current URL: {self._current_url()}")
                raise UiLoginAttemptFailedException("No Google auth cookies were present", "login_to_google_meet_account")

        logger.info(f"After waiting, URL is {self._current_url()}")

    def has_google_cookies_that_indicate_logged_in(self, driver) -> bool:
        google_auth_cookie_names = {
            "SID",
            "HSID",
            "SSID",
            "APISID",
            "SAPISID",
            "__Secure-1PSID",
            "__Secure-3PSID",
            "__Secure-1PAPISID",
            "__Secure-3PAPISID",
            "SIDCC",
        }

        cookies = self._get_cookies()
        names = {c.get("name") for c in cookies if c.get("name")}
        any_google_auth_cookies_present = bool(names & google_auth_cookie_names)
        logger.warning(f"Cookie names: {names}. Any Google auth cookies present: {any_google_auth_cookies_present}.")
        return any_google_auth_cookies_present

    def attempt_to_join_meeting(self):
        if self.google_meet_bot_login_is_available and self.google_meet_bot_login_should_be_used:
            self.login_to_google_meet_account_with_retries()

        layout_to_select = self.get_layout_to_select()

        self._navigate(self.meeting_url)

        parsed_url = urlparse(self.meeting_url)
        meeting_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": meeting_origin,
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
            selector_type=By.XPATH,
            selector=self.join_now_button_selector(),
            wait_time_seconds=60,
            visible_only=True,
        )
        self.bezier_mouse_move_to_target_element(join_button)
        time.sleep(random.uniform(0.1, 0.3))
        logger.info("Clicking the join button...")
        self.click_element(join_button, "join_button")

        self.click_captions_button()
        self.wait_for_host_if_needed()
        self.set_layout(layout_to_select)

        if self.disable_incoming_video:
            self.disable_incoming_video_in_ui()

        if self.google_meet_closed_captions_language:
            self.select_language(self.google_meet_closed_captions_language)

        if os.getenv("DO_NOT_RECORD_MEETING_REACTIONS") == "true":
            self.turn_off_reactions()

        self.ready_to_show_bot_image()

    def scroll_element_into_view(self, element, step):
        try:
            self._scroll_element_into_view(element)
            logger.info(f"Scrolled element into view for {step}")
        except Exception as e:
            logger.warning(f"Error scrolling element into view for {step}")
            raise UiCouldNotLocateElementException(
                "Error scrolling element into view",
                step,
                e,
            )

    def select_language(self, language):
        logger.info(f"Selecting language: {language}")
        logger.info("Waiting for the more options button...")
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            selector_type=By.CSS_SELECTOR,
            selector='button[jsname="NakZHc"][aria-label="More options"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            selector_type=By.XPATH,
            selector='//li[.//span[text()="Settings"]]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "settings_list_item")

        logger.info("Waiting for the captions button")
        self.locate_element(
            step="captions_button",
            selector_type=By.CSS_SELECTOR,
            selector='button[jsname="z4Tpl"][aria-label="Captions"]',
            wait_time_seconds=6,
            visible_only=True,
        )

        click_language_option_result = self.driver.execute_script("return clickLanguageOption(arguments[0]);", language)
        logger.info(f"click_language_option_result: {click_language_option_result}")
        if not click_language_option_result:
            raise UiCouldNotLocateElementException(f"Could not find language option {language}", "language_option")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button_for_language_selection",
            selector_type=By.CSS_SELECTOR,
            selector='button[aria-label="Close dialog"]',
            wait_time_seconds=6,
            visible_only=True,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def click_leave_button(self):
        logger.info("Waiting for the leave button")
        num_attempts = 5
        for attempt_index in range(num_attempts):
            leave_button = self.locate_element(
                step="leave_button",
                selector_type=By.CSS_SELECTOR,
                selector='button[jsname="CQylAd"][aria-label="Leave call"]',
                wait_time_seconds=16,
                visible_only=True,
            )
            logger.info("Clicking the leave button")
            try:
                self.click_element(leave_button, "leave_button")
                return
            except Exception as e:
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e
                logger.warning("Error clicking leave button. Retrying...")