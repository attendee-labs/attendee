import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter

TEST_TIMEOUT_SECONDS = 0.2


def build_driver(*, pid=4242, user_data_dir=None, hang_event=None):
    """A stand-in for a selenium driver. If hang_event is given, close() blocks on it."""
    driver = MagicMock()
    if pid is None:
        driver.service.process = None
    else:
        driver.service.process.pid = pid
    driver.capabilities = {"chrome": {"userDataDir": user_data_dir}} if user_data_dir else {}
    if hang_event is not None:
        driver.close.side_effect = lambda: hang_event.wait()
    return driver


def build_adapter(driver):
    """A real WebBotAdapter with its methods, without running the 25-arg __init__.

    __init__ only assigns attributes (it never launches selenium or a display), and the
    teardown paths only read self.driver, so __new__ + setting driver is enough to exercise
    the actual code on a genuine instance."""
    adapter = WebBotAdapter.__new__(WebBotAdapter)
    adapter.driver = driver
    return adapter


def hanging_event(test_case):
    """An event that blocks a call until the test finishes."""
    event = threading.Event()
    test_case.addCleanup(event.set)
    return event


class DriverTeardownLogicTest(unittest.TestCase):
    """The control flow of teardown_driver. The process-killing side effects are stubbed here
    (see DriverTeardownProcessTreeTest for the real thing); these tests exist to pin down the
    ordering, the timeout-abandonment behavior, and the error handling that the integration
    tests can't easily provoke."""

    def run_teardown(self, adapter, *, graceful_shutdown_fn=None, timeout=TEST_TIMEOUT_SECONDS):
        if graceful_shutdown_fn is None:
            graceful_shutdown_fn = adapter.default_graceful_driver_shutdown
        # Stub the things that touch real processes so a made-up pid can never hurt this machine.
        with (
            patch("bots.web_bot_adapter.web_bot_adapter.os.kill"),
            patch("bots.web_bot_adapter.web_bot_adapter.subprocess.run"),
            patch.object(adapter, "_descendant_pids", return_value=[]),
        ):
            started_at = time.monotonic()
            adapter.teardown_driver(graceful_shutdown_fn=graceful_shutdown_fn, graceful_timeout_seconds=timeout)
            return time.monotonic() - started_at

    def test_responsive_driver_is_closed_and_cleared(self):
        driver = build_driver()
        adapter = build_adapter(driver)

        elapsed = self.run_teardown(adapter)

        driver.close.assert_called_once()
        driver.quit.assert_called_once()
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS)

    def test_hung_graceful_shutdown_is_abandoned_at_the_timeout(self):
        driver = build_driver(hang_event=hanging_event(self))
        adapter = build_adapter(driver)

        elapsed = self.run_teardown(adapter)

        # We give up on the graceful path instead of hanging on the wedged driver forever.
        self.assertGreaterEqual(elapsed, TEST_TIMEOUT_SECONDS)
        self.assertLess(elapsed, TEST_TIMEOUT_SECONDS * 10)
        driver.quit.assert_not_called()

    def test_missing_chromedriver_process_does_not_raise(self):
        driver = build_driver(pid=None, hang_event=hanging_event(self))
        adapter = build_adapter(driver)

        self.run_teardown(adapter)  # must not raise

    def test_no_driver_returns_early(self):
        adapter = build_adapter(None)

        adapter.teardown_driver(graceful_shutdown_fn=adapter.default_graceful_driver_shutdown)  # must not raise

    def test_errors_from_close_and_quit_are_swallowed(self):
        driver = build_driver()
        driver.close.side_effect = RuntimeError("boom")
        driver.quit.side_effect = RuntimeError("boom")
        adapter = build_adapter(driver)

        self.run_teardown(adapter)  # must not raise

        driver.quit.assert_called_once()


class GracefulShutdownStrategiesTest(unittest.TestCase):
    """The graceful shutdown strategies passed to teardown_driver."""

    def test_default_shutdown_closes_then_quits(self):
        driver = build_driver()
        adapter = build_adapter(driver)

        adapter.default_graceful_driver_shutdown(driver)

        driver.close.assert_called_once()
        driver.quit.assert_called_once()

    def test_cleanup_shutdown_runs_hook_before_close_and_quit(self):
        calls = []
        driver = build_driver()
        driver.close.side_effect = lambda: calls.append("close")
        driver.quit.side_effect = lambda: calls.append("quit")
        adapter = build_adapter(driver)
        adapter.subclass_specific_before_driver_close = lambda driver: calls.append("hook")

        with patch.object(adapter, "log_browser_history") as log_browser_history:
            adapter.cleanup_graceful_driver_shutdown(driver)

        log_browser_history.assert_called_once_with(driver=driver)
        self.assertEqual(calls, ["hook", "close", "quit"])

    def test_cleanup_shutdown_still_quits_when_hook_raises(self):
        driver = build_driver()
        adapter = build_adapter(driver)

        def boom(driver):
            raise RuntimeError("boom")

        adapter.subclass_specific_before_driver_close = boom

        with patch.object(adapter, "log_browser_history"):
            adapter.cleanup_graceful_driver_shutdown(driver)  # must not raise

        # The hook and close() share a try block, so a failing hook skips close but quit still runs.
        driver.close.assert_not_called()
        driver.quit.assert_called_once()


class AbortJoinAttemptTest(unittest.TestCase):
    """abort_join_attempt closes the driver directly without quitting it or reaping the tree."""

    def test_closes_driver_without_quitting(self):
        driver = build_driver()
        adapter = build_adapter(driver)

        adapter.abort_join_attempt()

        driver.close.assert_called_once()
        driver.quit.assert_not_called()

    def test_swallows_close_error(self):
        driver = build_driver()
        driver.close.side_effect = RuntimeError("boom")
        adapter = build_adapter(driver)

        adapter.abort_join_attempt()  # must not raise


def _process_alive(pid):
    """True if the pid names a live (non-zombie) process. Reads /proc so a reaped-but-not-yet
    -waited zombie counts as dead, which is what we care about here."""
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            state = stat_file.read().rsplit(") ", 1)[1].split(" ", 1)[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def _wait_until_dead(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(sys.platform.startswith("linux"), "teardown_driver's process reaping is Linux-only")
class DriverTeardownProcessTreeTest(unittest.TestCase):
    """Integration test: teardown_driver must actually kill the real chromedriver process tree.

    Launching a real chromedriver would require Chrome, so we stand up an equivalent tree: a
    parent shell that spawns child processes. teardown_driver reads the parent pid exactly the
    way it reads chromedriver's, walks the real /proc tree via `ps`, and SIGKILLs everything.
    Nothing here is mocked, so this catches wrong-pid, surviving-descendant, and pkill bugs.
    """

    def _reap(self, proc):
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _wait_for_descendants(self, adapter, pid, expected, timeout=5):
        deadline = time.monotonic() + timeout
        descendants = []
        while time.monotonic() < deadline:
            descendants = adapter._descendant_pids(pid)
            if len(descendants) >= expected:
                return descendants
            time.sleep(0.02)
        return descendants

    def test_real_process_tree_is_reaped(self):
        # A parent shell that spawns two child sleeps and waits on them -> a real 3-process tree.
        parent = subprocess.Popen(["sh", "-c", "sleep 300 & sleep 300 & wait"])
        self.addCleanup(self._reap, parent)

        driver = MagicMock()
        driver.service.process.pid = parent.pid
        driver.capabilities = {}
        adapter = build_adapter(driver)

        descendants = self._wait_for_descendants(adapter, parent.pid, expected=2)
        self.assertGreaterEqual(len(descendants), 2, "child processes never started")

        adapter.teardown_driver(graceful_shutdown_fn=adapter.default_graceful_driver_shutdown, graceful_timeout_seconds=TEST_TIMEOUT_SECONDS)

        for pid in [parent.pid, *descendants]:
            self.assertTrue(_wait_until_dead(pid), f"pid {pid} survived teardown")

    def test_reparented_process_is_pkilled_by_user_data_dir(self):
        # A process with no chromedriver pid attached; only the userDataDir pkill fallback can catch it.
        marker = f"attendee-teardown-test-{os.getpid()}-{time.time_ns()}"
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(300)  # {marker}"])
        self.addCleanup(self._reap, proc)

        self.assertTrue(_process_alive(proc.pid))

        driver = MagicMock()
        driver.service.process = None  # nothing for the pid path to kill
        driver.capabilities = {"chrome": {"userDataDir": marker}}
        adapter = build_adapter(driver)

        adapter.teardown_driver(graceful_shutdown_fn=adapter.default_graceful_driver_shutdown, graceful_timeout_seconds=TEST_TIMEOUT_SECONDS)

        self.assertTrue(_wait_until_dead(proc.pid), "pkill -f userDataDir did not kill the process")


CHROMEDRIVER_PATH = "/usr/local/bin/chromedriver"


@unittest.skipUnless(sys.platform.startswith("linux"), "teardown_driver's process reaping is Linux-only")
@unittest.skipUnless(os.path.exists(CHROMEDRIVER_PATH), f"chromedriver not installed at {CHROMEDRIVER_PATH}")
class DriverTeardownRealChromeTest(unittest.TestCase):
    """The most realistic teardown test: launch an actual chromedriver + Chrome the same way the
    adapter does, then prove teardown_driver kills the whole real tree. Beyond the /proc stand-in,
    this verifies we read the pid and userDataDir out of a genuine selenium driver correctly, so it
    would catch selenium changing the shape of driver.service.process or driver.capabilities.
    Skipped automatically wherever chromedriver isn't installed (e.g. most laptops)."""

    def _start_display(self):
        if os.environ.get("DISPLAY") is not None:
            return
        try:
            from pyvirtualdisplay import Display
        except ImportError:
            self.skipTest("pyvirtualdisplay not available")
        display = Display(visible=0, size=(1920, 1080))
        display.start()
        self.addCleanup(display.stop)

    def _launch_real_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        options = webdriver.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        return webdriver.Chrome(options=options, service=Service(executable_path=CHROMEDRIVER_PATH))

    def test_real_chrome_tree_is_reaped(self):
        self._start_display()

        try:
            driver = self._launch_real_driver()
        except Exception as e:
            # Launching Chrome is covered by test_can_open_chrome; if the environment can't even
            # start it, there's nothing meaningful for a teardown test to assert.
            self.skipTest(f"could not launch chrome: {e}")

        adapter = build_adapter(driver)
        # Guarantee the browser is gone even if an assertion below fails before teardown runs.
        self.addCleanup(lambda: adapter.driver and adapter.teardown_driver(graceful_shutdown_fn=adapter.default_graceful_driver_shutdown, graceful_timeout_seconds=5))

        chromedriver_pid = driver.service.process.pid
        descendants = adapter._descendant_pids(chromedriver_pid)
        self.assertTrue(descendants, "expected chrome to spawn child processes under chromedriver")

        adapter.teardown_driver(graceful_shutdown_fn=adapter.default_graceful_driver_shutdown, graceful_timeout_seconds=5)

        for pid in [chromedriver_pid, *descendants]:
            self.assertTrue(_wait_until_dead(pid), f"pid {pid} survived teardown")


if __name__ == "__main__":
    unittest.main()
