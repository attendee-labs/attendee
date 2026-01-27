import os
import sys

import dj_database_url

from .base import *
from .base import LOG_FORMATTERS

DEBUG = True
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=os.getenv("POSTGRES_SSL_REQUIRE", "true") == "true",
    ),
}

# PRESERVE CELERY TASKS IF WORKER IS SHUT DOWN
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# Configuration SSL/HTTPS
SSL_REQUIRE = os.getenv("DJANGO_SSL_REQUIRE", "false") == "true"
if SSL_REQUIRE:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
else:
    SECURE_PROXY_SSL_HEADER = None
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

if os.getenv("DISABLE_EMAIL", "false") != "true":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.mailgun.org")
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@mail.attendee.dev")

ADMINS = []

if os.getenv("ERROR_REPORTS_RECEIVER_EMAIL_ADDRESS"):
    ADMINS.append(
        (
            "Attendee Error Reports Email Receiver",
            os.getenv("ERROR_REPORTS_RECEIVER_EMAIL_ADDRESS"),
        )
    )

SERVER_EMAIL = os.getenv("SERVER_EMAIL", "noreply@mail.attendee.dev")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": LOG_FORMATTERS,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "formatter": os.getenv("ATTENDEE_LOG_FORMAT"),  # `None` (default formatter) is the default
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "xmlschema": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}
