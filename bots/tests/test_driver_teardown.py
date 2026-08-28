import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter

TEST_TIMEOUT_SECONDS = 0.2


def build_driver(*, hang_event=None, process=None):
    """A stand-in for a selenium driver. If hang_event is given, close() blocks on it."""
    driver = MagicMock()
    driver.service.process = process
    if hang_event is not None:
        driver.close.side_effect = lambda: hang_event.wait()
    return driver


def teardown(driver):
    # teardown_driver_with_timeout does not use self, so a stub instance is enough.
    with patch("bots.web_bot_adapter.web_bot_adapter.DRIVER_TEARDOWN_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
        started_at = time.monotonic()
        WebBotAdapter.teardown_driver_with_timeout(MagicMock(), driver)
        return time.monotonic() - started_at


class DriverTeardownTest(unittest.TestCase):
    def test_responsive_driver_is_closed_gracefully(self):
        process = MagicMock()
        process.poll.return_value = None
        driver = build_driver(process=process)

        elapsed = teardown(driver)

        driver.close.assert_called_once()
        driver.quit.assert_called_once()
        process.kill.assert_not_called()
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS)

    def test_hung_driver_is_abandoned_and_chromedriver_killed(self):
        hang_event = threading.Event()
        self.addCleanup(hang_event.set)
        process = MagicMock()
        process.poll.return_value = None
        driver = build_driver(hang_event=hang_event, process=process)

        elapsed = teardown(driver)

        # We give up on the graceful path instead of waiting out selenium's timeouts.
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS * 10)
        process.kill.assert_called_once()
        driver.quit.assert_not_called()

    def test_already_exited_chromedriver_is_not_killed_again(self):
        hang_event = threading.Event()
        self.addCleanup(hang_event.set)
        process = MagicMock()
        process.poll.return_value = 0
        driver = build_driver(hang_event=hang_event, process=process)

        teardown(driver)

        process.kill.assert_not_called()

    def test_missing_chromedriver_process_does_not_raise(self):
        hang_event = threading.Event()
        self.addCleanup(hang_event.set)
        driver = build_driver(hang_event=hang_event, process=None)

        teardown(driver)  # must not raise

    def test_errors_from_close_and_quit_are_swallowed(self):
        driver = build_driver(process=MagicMock())
        driver.close.side_effect = RuntimeError("boom")
        driver.quit.side_effect = RuntimeError("boom")

        teardown(driver)  # must not raise

        driver.quit.assert_called_once()
