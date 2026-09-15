# bots/bot_controller/main_thread_executor.py
import functools
import threading

from gi.repository import GLib


class MainThreadExecutor:
    """Runs callables on the GLib main loop thread and blocks the caller until done."""

    def __init__(self):
        # Held as a Thread object rather than an ident, since idents are recycled
        # once a thread exits and another thread could then look like the main one.
        self._main_thread = threading.current_thread()

    def run(self, func, *args, timeout_seconds=60, **kwargs):
        # If we're already on the main thread, just call it. This avoids
        # deadlocking when the same callback is invoked from the main loop.
        if threading.current_thread() is self._main_thread:
            return func(*args, **kwargs)

        done = threading.Event()
        outcome = {}

        def invoke():
            try:
                outcome["value"] = func(*args, **kwargs)
            except BaseException as e:
                outcome["error"] = e
            finally:
                done.set()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(invoke)

        if not done.wait(timeout_seconds):
            raise TimeoutError(f"Timed out waiting for {getattr(func, '__name__', func)} to run on main thread")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def wraps(self, func, timeout_seconds=120):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            return self.run(func, *args, timeout_seconds=timeout_seconds, **kwargs)

        return wrapped
