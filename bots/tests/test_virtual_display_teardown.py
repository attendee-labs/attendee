import shutil
import sys
import time
import unittest
from unittest.mock import MagicMock

from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter


def build_adapter(*, display, debug_screen_recorder=None, stop_recording_screen_callback=None):
    """A real WebBotAdapter with its methods, without running the 25-arg __init__.

    __init__ only assigns attributes (it never launches selenium or a display), and cleanup()
    only reads the attributes set below, so __new__ + setting them is enough to exercise the
    actual code on a genuine instance. Note __init__ never assigns self.display at all - it's
    only set in init() when no DISPLAY env var exists - so omitting it is a real state."""
    adapter = WebBotAdapter.__new__(WebBotAdapter)
    adapter.stop_recording_screen_callback = stop_recording_screen_callback
    adapter.driver = None
    adapter.last_websocket_message_processed_time = None
    adapter.debug_screen_recorder = debug_screen_recorder if debug_screen_recorder is not None else MagicMock()
    adapter.websocket_server = None
    if display is not None:
        adapter.display = display
    return adapter


class VirtualDisplayTeardownLogicTest(unittest.TestCase):
    """The control flow of cleanup() around the virtual display. The display is a mock here (see
    VirtualDisplayTeardownProcessTest for the real Xvfb); these tests pin down the ordering and
    the error handling that keeps one failing teardown step from skipping the rest - cleanup()
    runs at most once, because BotController.cleanup() guards it with cleaned_up."""

    def test_display_is_stopped_during_cleanup(self):
        display = MagicMock()
        adapter = build_adapter(display=display)

        adapter.cleanup()

        display.stop.assert_called_once()

    def test_display_is_stopped_after_the_debug_screen_recorder(self):
        calls = []
        display = MagicMock()
        display.stop.side_effect = lambda: calls.append("display")
        recorder = MagicMock()
        recorder.stop.side_effect = lambda: calls.append("recorder")
        adapter = build_adapter(display=display, debug_screen_recorder=recorder)

        adapter.cleanup()

        # The debug recorder is an x11grab ffmpeg reading this display, so it must stop first.
        self.assertEqual(calls, ["recorder", "display"])

    def test_cleanup_completes_when_display_stop_raises(self):
        display = MagicMock()
        display.stop.side_effect = RuntimeError("boom")
        adapter = build_adapter(display=display)

        adapter.cleanup()  # must not raise

        display.stop.assert_called_once()
        self.assertTrue(adapter.cleaned_up)

    def test_display_is_still_stopped_when_debug_screen_recorder_raises(self):
        display = MagicMock()
        recorder = MagicMock()
        recorder.stop.side_effect = RuntimeError("boom")
        adapter = build_adapter(display=display, debug_screen_recorder=recorder)

        adapter.cleanup()  # must not raise

        display.stop.assert_called_once()
        self.assertTrue(adapter.cleaned_up)

    def test_display_is_still_stopped_when_stop_recording_callback_raises(self):
        display = MagicMock()
        stop_recording_screen_callback = MagicMock(side_effect=RuntimeError("boom"))
        adapter = build_adapter(display=display, stop_recording_screen_callback=stop_recording_screen_callback)

        adapter.cleanup()  # must not raise

        stop_recording_screen_callback.assert_called_once()
        display.stop.assert_called_once()
        self.assertTrue(adapter.cleaned_up)

    def test_cleanup_completes_when_there_is_no_display_attribute(self):
        adapter = build_adapter(display=None)

        self.assertFalse(hasattr(adapter, "display"))

        adapter.cleanup()  # must not raise

        self.assertTrue(adapter.cleaned_up)


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


@unittest.skipUnless(sys.platform.startswith("linux"), "Xvfb teardown is Linux-only")
@unittest.skipUnless(shutil.which("Xvfb"), "Xvfb is not installed")
class VirtualDisplayTeardownProcessTest(unittest.TestCase):
    """Integration test: cleanup() must actually kill the real Xvfb process. Nothing here is
    mocked, so this catches a display that is only detached rather than stopped. The CI image
    installs xvfb and xauth (Dockerfile:50), so this runs in CI - a start failure is a failure,
    never a skip, otherwise the test would pass having proved nothing."""

    def test_real_xvfb_process_is_stopped(self):
        from pyvirtualdisplay import Display

        display = Display(visible=0, size=(1930, 1090), use_xauth=True)
        display.start()
        self.addCleanup(lambda: _process_alive(pid) and display.stop())
        pid = display.pid
        self.assertTrue(_process_alive(pid), "Xvfb never started")

        adapter = build_adapter(display=display)
        adapter.cleanup()

        self.assertTrue(_wait_until_dead(pid), f"Xvfb pid {pid} survived cleanup")


if __name__ == "__main__":
    unittest.main()
