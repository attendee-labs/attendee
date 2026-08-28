import threading
import time
import types
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


def build_adapter(driver):
    """The smallest object the teardown methods need: a driver and the real timeout helper."""
    adapter = types.SimpleNamespace(driver=driver)
    adapter.run_driver_call_with_timeout = types.MethodType(WebBotAdapter.run_driver_call_with_timeout, adapter)
    return adapter


def teardown(driver, before_close=None):
    """Run teardown_driver_with_timeout with a short timeout, returning how long it took."""
    with patch("bots.web_bot_adapter.web_bot_adapter.DRIVER_TEARDOWN_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
        started_at = time.monotonic()
        WebBotAdapter.teardown_driver_with_timeout(build_adapter(driver), driver, before_close=before_close)
        return time.monotonic() - started_at


def hanging_event(test_case):
    """An event that blocks a call until the test finishes."""
    event = threading.Event()
    test_case.addCleanup(event.set)
    return event


def live_process():
    process = MagicMock()
    process.poll.return_value = None
    return process


class DriverTeardownTest(unittest.TestCase):
    def test_responsive_driver_is_closed_gracefully(self):
        process = live_process()
        driver = build_driver(process=process)

        elapsed = teardown(driver)

        driver.close.assert_called_once()
        driver.quit.assert_called_once()
        process.kill.assert_not_called()
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS)

    def test_hung_driver_is_abandoned_and_chromedriver_killed(self):
        process = live_process()
        driver = build_driver(hang_event=hanging_event(self), process=process)

        elapsed = teardown(driver)

        # We give up on the graceful path instead of waiting out selenium's timeouts.
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS * 10)
        process.kill.assert_called_once()
        driver.quit.assert_not_called()

    def test_already_exited_chromedriver_is_not_killed_again(self):
        process = MagicMock()
        process.poll.return_value = 0
        driver = build_driver(hang_event=hanging_event(self), process=process)

        teardown(driver)

        process.kill.assert_not_called()

    def test_missing_chromedriver_process_does_not_raise(self):
        driver = build_driver(hang_event=hanging_event(self), process=None)

        teardown(driver)  # must not raise

    def test_errors_from_close_and_quit_are_swallowed(self):
        driver = build_driver(process=live_process())
        driver.close.side_effect = RuntimeError("boom")
        driver.quit.side_effect = RuntimeError("boom")

        teardown(driver)  # must not raise

        driver.quit.assert_called_once()


class BeforeCloseHookTest(unittest.TestCase):
    """cleanup() passes a before_close hook; it must not be able to wedge the teardown."""

    def test_hook_runs_before_close_and_quit(self):
        calls = []
        driver = build_driver(process=live_process())
        driver.close.side_effect = lambda: calls.append("close")
        driver.quit.side_effect = lambda: calls.append("quit")

        teardown(driver, before_close=lambda: calls.append("hook"))

        self.assertEqual(calls, ["hook", "close", "quit"])

    def test_failing_hook_does_not_prevent_teardown(self):
        driver = build_driver(process=live_process())

        def boom():
            raise RuntimeError("boom")

        teardown(driver, before_close=boom)

        driver.close.assert_called_once()
        driver.quit.assert_called_once()

    def test_hung_hook_still_kills_chromedriver(self):
        process = live_process()
        driver = build_driver(process=process)

        teardown(driver, before_close=hanging_event(self).wait)

        process.kill.assert_called_once()
        driver.close.assert_not_called()


class AbortJoinAttemptTest(unittest.TestCase):
    """abort_join_attempt runs on the join path, so it must not block on a wedged driver."""

    def abort(self, driver):
        with patch("bots.web_bot_adapter.web_bot_adapter.DRIVER_TEARDOWN_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
            started_at = time.monotonic()
            WebBotAdapter.abort_join_attempt(build_adapter(driver))
            return time.monotonic() - started_at

    def test_closes_responsive_driver(self):
        driver = build_driver(process=live_process())

        self.abort(driver)

        driver.close.assert_called_once()

    def test_returns_promptly_when_close_hangs(self):
        driver = build_driver(hang_event=hanging_event(self), process=live_process())

        elapsed = self.abort(driver)

        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS * 10)

    def test_close_error_is_swallowed(self):
        driver = build_driver(process=live_process())
        driver.close.side_effect = RuntimeError("boom")

        self.abort(driver)  # must not raise
