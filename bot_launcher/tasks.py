import logging
import os

import django
from celery import shared_task

# Setup Django to access models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "attendee.settings")
django.setup()

from bots.models import Bot

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="bot_launcher.launch_bot_from_queue", queue="bot_launcher")
def launch_bot_from_queue(self, bot_id: int):
    """
    Launches an ephemeral Docker container to execute a bot.
    Timeout is calculated from bot's max_uptime_seconds + 1h margin.
    If max_uptime_seconds is not defined, uses default (4h).
    Container auto-removes on exit.
    Celery task returns in ~2 seconds.
    """
    # Lazy import of docker to avoid import errors in workers that don't need it
    try:
        import docker
    except ImportError:
        logger.error("docker module not available. Cannot launch ephemeral containers.")
        raise ImportError("docker module is required for ephemeral bot containers. Install it with: pip install docker==7.1.0")

    logger.info(f"Launching ephemeral Docker container for bot {bot_id}")

    try:
        # Connect to Docker daemon
        client = docker.from_env()

        # Check maximum simultaneous bots limit
        max_simultaneous_bots = int(os.getenv("BOT_MAX_SIMULTANEOUS_BOTS", "100"))
        running_containers = client.containers.list(
            filters={"label": "attendee.type=ephemeral-bot", "status": "running"}
        )
        current_running_count = len(running_containers)

        if current_running_count >= max_simultaneous_bots:
            error_msg = (
                f"Maximum simultaneous bots limit reached: {current_running_count}/{max_simultaneous_bots}. "
                f"Cannot launch bot {bot_id}"
            )
            logger.error(error_msg)
            raise Exception(error_msg)

        logger.info(
            f"Current running bots: {current_running_count}/{max_simultaneous_bots}. "
            f"Launching bot {bot_id}"
        )

        # Image to use (same as worker)
        image = os.getenv("BOT_CONTAINER_IMAGE", "attendee-attendee-worker-local:latest")

        # Copy all environment variables from worker to container
        # This ensures all env vars (DB, Redis, AWS, Deepgram, etc.) are automatically passed
        env_vars = os.environ.copy()
        
        # Remove Docker-specific or worker-specific vars that shouldn't be in the bot container
        vars_to_exclude = {
            "BOT_CONTAINER_IMAGE",  # Only needed by launcher
            "BOT_MEMORY_LIMIT",  # Only needed by launcher
            "BOT_CPU_QUOTA",  # Only needed by launcher
            "BOT_CPU_PERIOD",  # Only needed by launcher
            "BOT_MAX_EXECUTION_SECONDS",  # Only needed by launcher
            "BOT_MAX_SIMULTANEOUS_BOTS",  # Only needed by launcher
        }
        env_vars = {k: v for k, v in env_vars.items() if k not in vars_to_exclude}

        # Container name
        container_name = f"bot-{bot_id}"

        # Resource limits per bot (configurable)
        mem_limit = os.getenv("BOT_MEMORY_LIMIT", "2g")  # 2GB default
        cpu_quota = int(os.getenv("BOT_CPU_QUOTA", "100000"))  # 1 CPU default (100000 = 1 core)
        cpu_period = int(os.getenv("BOT_CPU_PERIOD", "100000"))

        # Get bot to retrieve max_uptime_seconds
        try:
            bot = Bot.objects.get(id=bot_id)
            automatic_leave_settings = bot.automatic_leave_settings()
            bot_max_uptime = automatic_leave_settings.get("max_uptime_seconds")
            
            # Calculate timeout = max_uptime_seconds + 1h (3600s) if defined, otherwise use default
            if bot_max_uptime is not None:
                max_execution_seconds = bot_max_uptime + 3600  # + 1h margin
                logger.info(f"Bot {bot_id} has max_uptime_seconds={bot_max_uptime}, setting container timeout to {max_execution_seconds}s")
            else:
                # No max_uptime defined, use default (4h)
                max_execution_seconds = int(os.getenv("BOT_MAX_EXECUTION_SECONDS", "14400"))
                logger.info(f"Bot {bot_id} has no max_uptime_seconds, using default timeout {max_execution_seconds}s")
        except Bot.DoesNotExist:
            logger.warning(f"Bot {bot_id} not found in database, using default timeout")
            max_execution_seconds = int(os.getenv("BOT_MAX_EXECUTION_SECONDS", "14400"))
        except Exception as e:
            logger.error(f"Error retrieving bot {bot_id} for timeout calculation: {e}, using default")
            max_execution_seconds = int(os.getenv("BOT_MAX_EXECUTION_SECONDS", "14400"))

        # Command to execute in container with timeout
        # timeout forces stop after max_execution_seconds
        command = f"timeout {max_execution_seconds} python manage.py run_bot --botid {bot_id}"

        # Labels for identification
        labels = {
            "attendee.type": "ephemeral-bot",
            "attendee.bot_id": str(bot_id),
        }

        # Launch ephemeral container
        container = client.containers.run(
            image=image,
            command=command,
            name=container_name,
            detach=True,  # Detached so task returns quickly
            remove=True,  # Auto-remove on exit
            environment=env_vars,
            labels=labels,
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            cpu_period=cpu_period,
            network_mode="host",  # Same network mode as workers
            security_opt=["seccomp=unconfined"],  # Same config as workers
            stop_timeout=max_execution_seconds + 60,  # Stop timeout (max_execution + 1min margin)
            # Container will automatically stop after max_execution_seconds thanks to timeout in command
        )

        logger.info(
            f"Ephemeral container {container_name} (ID: {container.short_id}) started for bot {bot_id}. "
            f"Container will auto-remove when bot execution completes."
        )

        return {
            "container_id": container.short_id,
            "container_name": container_name,
            "bot_id": bot_id,
            "status": "started",
        }

    except docker.errors.ImageNotFound:
        logger.error(f"Image {image} not found. Cannot launch bot {bot_id}")
        raise
    except docker.errors.APIError as e:
        logger.error(f"Docker API error while launching bot {bot_id}: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error launching ephemeral container for bot {bot_id}: {str(e)}", exc_info=True)
        raise
