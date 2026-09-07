import os

from .base import *
from .base import LOG_FORMATTERS, LOG_HANDLER_NAMES, LOG_HANDLERS
from .db import default_database

DEBUG = False
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")

DATABASES = {
    "default": default_database(ssl_require=True),
}

# PRESERVE CELERY TASKS IF WORKER IS SHUT DOWN
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Disabling these because it's enforced at the ingress level on GKE
# SECURE_SSL_REDIRECT = True
# SECURE_HSTS_SECONDS = 60
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# No email on staging by default
if os.getenv("DISABLE_EMAIL", "true") != "true":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.mailgun.org")
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@mail.attendee.dev")

ADMINS = []

SERVER_EMAIL = os.getenv("SERVER_EMAIL", "noreply@mail.attendee.dev")

CSRF_TRUSTED_ORIGINS = os.getenv("CSRF_TRUSTED_ORIGINS", "https://*.attendee.dev").split(",")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": LOG_FORMATTERS,
    "handlers": LOG_HANDLERS,
    "root": {
        "handlers": LOG_HANDLER_NAMES,
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": LOG_HANDLER_NAMES,
            "level": "INFO",
            "propagate": False,
        },
        "xmlschema": {"level": "WARNING", "handlers": LOG_HANDLER_NAMES, "propagate": False},
    },
}
