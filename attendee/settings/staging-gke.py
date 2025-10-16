import logging
import os

import dj_database_url

from .base import *

DEBUG = False
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=True,
    ),
}

# PRESERVE CELERY TASKS IF WORKER IS SHUT DOWN
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Disabling these because it's enforced at the ingress level on GKE
# SECURE_SSL_REDIRECT = True
# SECURE_HSTS_SECONDS = 60
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# No email on staging
# EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
# EMAIL_HOST = "smtp.mailgun.org"
# EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")
# EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
# EMAIL_PORT = 587
# EMAIL_USE_TLS = True
# DEFAULT_FROM_EMAIL = "noreply@mail.attendee.dev"

ADMINS = []

SERVER_EMAIL = "noreply@mail.attendee.dev"

CSRF_TRUSTED_ORIGINS = ["https://*.attendee.dev"]

# Configure logging to write JSON to stdout for GCP Cloud Logging
# GCP Cloud Logging parses JSON and extracts the severity field


class JsonFormatter(logging.Formatter):
    """Format logs as JSON for GCP Cloud Logging severity parsing."""

    def format(self, record):
        import json
        from datetime import datetime

        log_obj = {
            "message": record.getMessage(),
            "severity": record.levelname,
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "logger": record.name,
        }

        # Add exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": JsonFormatter,
        },
    },
    "handlers": {
        "console_stdout": {
            "class": "bots.stdout_handler.StdoutStreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console_stdout"],
        "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console_stdout"],
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "bots": {
            "handlers": ["console_stdout"],
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "bots.bot_controller": {
            "handlers": ["console_stdout"],
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "bots.web_bot_adapter": {
            "handlers": ["console_stdout"],
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "bots.management": {
            "handlers": ["console_stdout"],
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
