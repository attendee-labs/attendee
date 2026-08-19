import threading
import time
import unittest

import gi

gi.require_version("GLib", "2.0")
import os
import subprocess
import sys
from pathlib import Path

from gi.repository import GLib  # noqa: E402

from bots.bot_controller.main_thread_executor import MainThreadExecutor  # noqa: E402


class IsolatedProcessTestCase(unittest.TestCase):
    """
    Runs each test method in a fresh Python/Django process.

    The parent Django test runner discovers the tests normally. For each test,
    this class launches:

        python manage.py test --keepdb <fully-qualified-test-name>

    The environment variable prevents the child from spawning another child.
    """

    _ISOLATED_PROCESS_ENV = "RUNNING_ISOLATED_TEST_PROCESS"

    def run(self, result=None):
        # In the child process, actually run the test normally.
        if os.environ.get(self._ISOLATED_PROCESS_ENV) == "1":
            return super().run(result)

        if result is None:
            result = self.defaultTestResult()

        test_name = self.id()

        env = os.environ.copy()
        env[self._ISOLATED_PROCESS_ENV] = "1"

        # Your normal invocation is `python manage.py test ...`, so sys.argv[0]
        # should be the path to manage.py.
        manage_py = Path(sys.argv[0]).resolve()

        proc = subprocess.run(
            [
                sys.executable,
                str(manage_py),
                "test",
                "--keepdb",
                test_name,
            ],
            env=env,
            cwd=manage_py.parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        result.startTest(self)

        try:
            if proc.returncode == 0:
                result.addSuccess(self)
            else:
                try:
                    raise AssertionError(f"Isolated test process failed (exit code {proc.returncode}):\n\n{proc.stdout}")
                except AssertionError:
                    result.addFailure(self, sys.exc_info())
        finally:
            result.stopTest(self)

        return result


class TestMainThreadExecutor(IsolatedProcessTestCase):
    """Exercises MainThreadExecutor against a real GLib main loop running on its own thread."""

    def setUp(self):
        self.main_loop = GLib.MainLoop()
        self.executor = None
        executor_created = threading.Event()

        def run_main_loop():
            # The executor must be created on the thread that owns the loop, since
            # that's the thread it will dispatch work to.
            self.executor = MainThreadExecutor()
            self.main_thread_id = threading.get_ident()
            GLib.idle_add(lambda: (executor_created.set(), GLib.SOURCE_REMOVE)[1])
            self.main_loop.run()

        self.main_loop_thread = threading.Thread(target=run_main_loop, daemon=True)
        self.main_loop_thread.start()
        self.assertTrue(executor_created.wait(timeout=5), "Main loop did not start")

    def tearDown(self):
        self.stop_main_loop()

    def stop_main_loop(self):
        if self.main_loop.is_running():
            GLib.idle_add(lambda: (self.main_loop.quit(), GLib.SOURCE_REMOVE)[1])
        self.main_loop_thread.join(timeout=5)
        self.assertFalse(self.main_loop_thread.is_alive(), "Main loop thread did not exit")

    def test_runs_on_main_loop_thread_and_returns_value(self):
        """A call from a worker thread is executed on the main loop thread."""
        ran_on = {}

        def add(a, b):
            ran_on["thread_id"] = threading.get_ident()
            return a + b

        result = {}
        worker = threading.Thread(target=lambda: result.update(value=self.executor.run(add, 2, 3)))
        worker.start()
        worker.join(timeout=5)

        self.assertEqual(result["value"], 5)
        self.assertEqual(ran_on["thread_id"], self.main_thread_id)
        self.assertNotEqual(ran_on["thread_id"], threading.get_ident())

    def test_exception_is_reraised_in_calling_thread(self):
        def blow_up():
            raise ValueError("boom")

        error = {}

        def call():
            try:
                self.executor.run(blow_up)
            except BaseException as e:
                error["exception"] = e

        worker = threading.Thread(target=call)
        worker.start()
        worker.join(timeout=5)

        self.assertIsInstance(error.get("exception"), ValueError)
        self.assertEqual(str(error["exception"]), "boom")

    def test_call_from_main_loop_thread_does_not_deadlock(self):
        """Nested calls from work already running on the loop thread run inline."""

        def inner():
            return threading.get_ident()

        def outer():
            # Would deadlock if this scheduled another idle callback and waited on it,
            # since the main loop is busy running outer().
            return self.executor.run(inner, timeout_seconds=2)

        result = {}
        worker = threading.Thread(target=lambda: result.update(value=self.executor.run(outer, timeout_seconds=5)))
        worker.start()
        worker.join(timeout=10)

        self.assertEqual(result.get("value"), self.main_thread_id)

    def test_concurrent_callers_each_get_their_own_result(self):
        def slow_double(n):
            time.sleep(0.01)
            return n * 2

        results = {}

        def call(n):
            results[n] = self.executor.run(slow_double, n)

        workers = [threading.Thread(target=call, args=(n,)) for n in range(10)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual(results, {n: n * 2 for n in range(10)})

    def test_wraps_dispatches_to_main_loop_thread(self):
        def get_thread_id_and_greet(name, greeting="hello"):
            return threading.get_ident(), f"{greeting} {name}"

        wrapped = self.executor.wraps(get_thread_id_and_greet)
        self.assertEqual(wrapped.__name__, "get_thread_id_and_greet")

        result = {}
        worker = threading.Thread(target=lambda: result.update(value=wrapped("world", greeting="hi")))
        worker.start()
        worker.join(timeout=5)

        self.assertEqual(result["value"], (self.main_thread_id, "hi world"))

    def test_times_out_when_main_loop_is_not_running(self):
        self.stop_main_loop()

        error = {}

        def call():
            try:
                self.executor.run(lambda: "never runs", timeout_seconds=0.2)
            except BaseException as e:
                error["exception"] = e

        worker = threading.Thread(target=call)
        worker.start()
        worker.join(timeout=5)

        self.assertIsInstance(error.get("exception"), TimeoutError)


if __name__ == "__main__":
    unittest.main()
