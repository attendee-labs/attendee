"""
Local development settings for running without Docker (e.g., on WSL).

This settings file is designed for running the application directly on your
local machine without Docker containers.

Usage:
    export DJANGO_SETTINGS_MODULE=attendee.settings.local
    python manage.py runserver
"""

import os

from .base import *

DEBUG = True
SITE_DOMAIN = "localhost:8000"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "tendee-stripe-hooks.ngrok.io"]

# Database - connects to local PostgreSQL
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "attendee_development"),
        "USER": os.getenv("POSTGRES_USER", "attendee_development_user"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "attendee_development_user"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

# Redis - connects to local Redis server
# Override the base.py REDIS_URL default for local development
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/5")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# Chrome sandbox setting for local development
ENABLE_CHROME_SANDBOX = os.getenv("ENABLE_CHROME_SANDBOX", "false").lower() == "true"

# Log more stuff in development
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "xmlschema": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        # Uncomment to log database queries
        # "django.db.backends": {
        #    "handlers": ["console"],
        #    "level": "DEBUG",
        #    "propagate": False,
        # },
    },
}
