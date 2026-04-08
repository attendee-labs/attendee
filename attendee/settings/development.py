import os

from .base import *

DEBUG = True

# Email configuration using Postmark SMTP (if token is provided)
if os.getenv("POSTMARK_API_TOKEN"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.postmarkapp.com"
    EMAIL_HOST_USER = os.getenv("POSTMARK_API_TOKEN")
    EMAIL_HOST_PASSWORD = os.getenv("POSTMARK_API_TOKEN")
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@oppy.pro")
    SERVER_EMAIL = os.getenv("SERVER_EMAIL", "noreply@oppy.pro")
SITE_DOMAIN = "localhost:8000"
ALLOWED_HOSTS = ["tendee-stripe-hooks.ngrok.io", "localhost", "127.0.0.1"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "attendee_development",
        "USER": "attendee_development_user",
        "PASSWORD": "attendee_development_user",
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": "5432",
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
