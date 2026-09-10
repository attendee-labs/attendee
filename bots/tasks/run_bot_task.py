import logging
import os
import signal

from celery import shared_task
from celery.signals import worker_shutting_down
from django.conf import settings

from bots.bot_controller import BotController

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def run_bot(self, bot_id):
    logger.info(f"Running bot {bot_id}")
    bot_controller = BotController(bot_id)
    bot_controller.run()


def kill_child_processes():
    # Get the process group ID (PGID) of the current process
    pgid = os.getpgid(os.getpid())

    try:
        # Send SIGTERM to all processes in the process group
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Process group may no longer exist


def shutting_down_handler(sig, how, exitcode, **kwargs):
    # When bots run inside the worker (LAUNCH_BOT_METHOD unset or "celery"), a bot task
    # spawns Chrome and other subprocesses that must not outlive the worker, so we
    # SIGTERM the whole process group as soon as the worker starts shutting down.
    logger.info("Celery worker shutting down, sending SIGTERM to all child processes")
    kill_child_processes()


# Only connect the handler when bots run in-process. Celery's response to SIGTERM is a
# warm shutdown: stop consuming and let in-flight tasks finish before exiting. This
# handler fires at the start of that warm shutdown and SIGTERMs every prefork child,
# so busy children die with WorkerLostError and their tasks are abandoned (and, with
# acks_late, requeued and re-run elsewhere). With the kubernetes and
# docker-compose-multi-host launch methods, run_bot never executes in the worker, so
# there are no bot subprocesses to clean up and the warm shutdown should be honoured
# so the pod's termination grace period is actually used.
if settings.BOTS_RUN_IN_CELERY_WORKER:
    worker_shutting_down.connect(shutting_down_handler)
