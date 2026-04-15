import logging
import os
import signal
import traceback

import rollbar
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_shutting_down

from bots.bot_controller import BotController
from bots.models import Bot, BotStates

logger = logging.getLogger(__name__)


def get_bot_context_for_rollbar(bot_id):
    """Get bot context for Rollbar error reporting."""
    try:
        bot = Bot.objects.get(id=bot_id)
        return {
            "bot_id": str(bot.id),
            "bot_object_id": bot.object_id,
            "bot_state": BotStates(bot.state).label if bot.state else "Unknown",
            "bot_state_value": bot.state,
            "meeting_url": bot.meeting_url,
            "project_id": str(bot.project_id) if bot.project_id else None,
            "created_at": bot.created_at.isoformat() if bot.created_at else None,
            "first_heartbeat": bot.first_heartbeat_timestamp,
            "last_heartbeat": bot.last_heartbeat_timestamp,
            "bot_duration_seconds": bot.bot_duration_seconds(),
            "session_type": bot.session_type,
            "join_at": bot.join_at.isoformat() if bot.join_at else None,
        }
    except Bot.DoesNotExist:
        return {"bot_id": str(bot_id), "error": "Bot not found in database"}
    except Exception as e:
        return {"bot_id": str(bot_id), "error": f"Failed to fetch bot context: {str(e)}"}


@shared_task(bind=True, soft_time_limit=3600, time_limit=3660)
def run_bot(self, bot_id):
    logger.info(f"Running bot {bot_id}")
    bot_controller = None
    try:
        bot_controller = BotController(bot_id)
        bot_controller.run()
    except SoftTimeLimitExceeded:
        logger.error(f"Bot {bot_id} exceeded soft time limit (3600 seconds)")

        # Get full context for Rollbar
        bot_context = get_bot_context_for_rollbar(bot_id)
        full_traceback = traceback.format_exc()

        # Report to Rollbar with full context
        rollbar.report_message(
            message=f"Bot {bot_id} exceeded soft time limit",
            level="error",
            extra_data={
                "bot_context": bot_context,
                "task_id": self.request.id,
                "task_name": self.name,
                "full_traceback": full_traceback,
                "timeout_type": "soft_time_limit",
                "timeout_seconds": 3600,
            },
        )

        # Attempt graceful cleanup if bot_controller was initialized
        if bot_controller:
            try:
                logger.info(f"Attempting graceful cleanup for bot {bot_id}")
                bot_controller.cleanup()
            except Exception as cleanup_error:
                logger.error(f"Error during cleanup after timeout for bot {bot_id}: {cleanup_error}")
                rollbar.report_exc_info(
                    extra_data={
                        "bot_id": str(bot_id),
                        "context": "cleanup_after_soft_time_limit",
                    }
                )

        # Re-raise to let Celery handle task failure
        raise


def kill_child_processes():
    # Get the process group ID (PGID) of the current process
    pgid = os.getpgid(os.getpid())

    try:
        # Send SIGTERM to all processes in the process group
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Process group may no longer exist


@worker_shutting_down.connect
def shutting_down_handler(sig, how, exitcode, **kwargs):
    # Just adding this code so we can see how to shut down all the tasks
    # when the main process is terminated.
    # It's likely overkill.
    logger.info("Celery worker shutting down, sending SIGTERM to all child processes")
    kill_child_processes()
