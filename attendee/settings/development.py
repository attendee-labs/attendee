import os

from .base import *

DEBUG = True
SITE_DOMAIN = "localhost:8000"
ALLOWED_HOSTS = ["tendee-stripe-hooks.ngrok.io", "localhost"]

# Support both PostgreSQL and MariaDB/MySQL via DB_ENGINE env var
DB_ENGINE = os.getenv("DB_ENGINE", "postgresql")  # Options: postgresql, mysql
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432" if DB_ENGINE == "postgresql" else "3306")
DB_NAME = os.getenv("DB_NAME", "attendee_development")
DB_USER = os.getenv("DB_USER", "attendee_development_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "attendee_development_user")

DATABASES = {
    "default": {
        "ENGINE": f"django.db.backends.{DB_ENGINE}",
        "NAME": DB_NAME,
        "USER": DB_USER,
        "PASSWORD": DB_PASSWORD,
        "HOST": DB_HOST,
        "PORT": DB_PORT,
        "OPTIONS": {"charset": "utf8mb4"} if DB_ENGINE == "mysql" else {},
    }
}

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
