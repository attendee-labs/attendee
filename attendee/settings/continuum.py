import os

from . import continuum_env

continuum_env.apply()

from .production import *  # noqa: E402,F403

# Namespace the default Celery queue. Attendee's routed queues are already
# named by env var (see base.py), but the default queue - the one run_bot and
# any unrouted task use - is literally "celery", which any other Celery app
# sharing the Redis logical DB also consumes from. Renaming it is what keeps
# the two apps from picking up each other's tasks.
# NOTE: workers must name the queues explicitly (-Q attendee,attendee-tasks);
# a worker with no -Q consumes "celery" and would sit idle.
CELERY_TASK_DEFAULT_QUEUE = os.getenv("CELERY_TASK_DEFAULT_QUEUE", "attendee")
