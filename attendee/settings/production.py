import os

from .base import *
from .base import LOG_FORMATTERS, LOG_HANDLER_NAMES, LOG_HANDLERS
from .db import default_database

DEBUG = False
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")

DATABASES = {
    "default": default_database(ssl_require=os.getenv("POSTGRES_SSL_REQUIRE", "true") == "true"),
}

# PRESERVE CELERY TASKS IF WORKER IS SHUT DOWN
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# Configuration SSL/HTTPS. Defaults to requiring SSL
DJANGO_SSL_REQUIRE = os.getenv("DJANGO_SSL_REQUIRE", "true") == "true"
if DJANGO_SSL_REQUIRE:
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

# Trusted origins for CSRF (e.g. behind a reverse proxy like Traefik).
# Defaults to https://<SITE_DOMAIN> when not explicitly set.
CSRF_TRUSTED_ORIGINS = os.getenv(
    "CSRF_TRUSTED_ORIGINS",
    f"https://{os.getenv('SITE_DOMAIN', 'app.attendee.dev')}",
).split(",")

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

CSRF_TRUSTED_ORIGINS = os.getenv("CSRF_TRUSTED_ORIGINS").split(",") if os.getenv("CSRF_TRUSTED_ORIGINS") else []

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": LOG_FORMATTERS,
    "handlers": LOG_HANDLERS,
    "root": {
        "handlers": LOG_HANDLER_NAMES,
        "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": LOG_HANDLER_NAMES,
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "xmlschema": {"level": "WARNING", "handlers": LOG_HANDLER_NAMES, "propagate": False},
    },
}
