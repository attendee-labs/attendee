import importlib
from unittest.mock import patch

from celery.signals import worker_shutting_down
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from bots.tasks import run_bot_task


def _handler_is_connected():
    return any(receiver() is run_bot_task.shutting_down_handler for _, receiver in worker_shutting_down.receivers if callable(receiver))


class WorkerShuttingDownHandlerConnectionTest(SimpleTestCase):
    """The handler is connected at import time, so reload the module under each setting value."""

    def _reload_with_setting(self, bots_run_in_celery_worker):
        worker_shutting_down.disconnect(run_bot_task.shutting_down_handler)
        with override_settings(BOTS_RUN_IN_CELERY_WORKER=bots_run_in_celery_worker):
            importlib.reload(run_bot_task)

    def tearDown(self):
        # Restore the module to the state matching the real settings so other tests are unaffected.
        self._reload_with_setting(settings.BOTS_RUN_IN_CELERY_WORKER)

    def test_handler_not_connected_when_bots_run_outside_worker(self):
        self._reload_with_setting(False)
        self.assertFalse(_handler_is_connected())

    def test_handler_connected_when_bots_run_in_worker(self):
        self._reload_with_setting(True)
        self.assertTrue(_handler_is_connected())

    def test_handler_calls_kill_child_processes(self):
        with patch.object(run_bot_task, "kill_child_processes") as mock_kill:
            run_bot_task.shutting_down_handler(sig="SIGTERM", how="Warm", exitcode=0)
        mock_kill.assert_called_once_with()
