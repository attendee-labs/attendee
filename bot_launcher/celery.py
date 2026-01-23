import logging
import os
import ssl

import django
from celery import Celery
from celery.signals import worker_ready

logger = logging.getLogger(__name__)

# Set the default Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "attendee.settings")

# Initialize Django
django.setup()

# Create the Celery app for bot launcher
if os.getenv("DISABLE_REDIS_SSL"):
    app = Celery(
        "bot_launcher",
        broker_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE},
        redis_backend_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE},
    )
else:
    app = Celery("bot_launcher")

# Load configuration from Django settings
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks
app.autodiscover_tasks()
