import logging
import os
import signal
import subprocess
import sys

from celery import shared_task
from celery.signals import worker_shutting_down

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def run_bot(self, bot_id):
    logger.info(f"Running bot {bot_id} in subprocess")
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "run_bot", "--botid", str(bot_id)],
    )
    proc.wait()

    if proc.returncode != 0:
        if proc.returncode < 0:
            sig_name = signal.Signals(-proc.returncode).name
            raise RuntimeError(f"Bot {bot_id} subprocess killed by {sig_name} (exit code {proc.returncode})")
        raise RuntimeError(f"Bot {bot_id} subprocess exited with code {proc.returncode}")


def kill_child_processes():
    pgid = os.getpgid(os.getpid())
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass


@worker_shutting_down.connect
def shutting_down_handler(sig, how, exitcode, **kwargs):
    logger.info("Celery worker shutting down, sending SIGTERM to all child processes")
    kill_child_processes()