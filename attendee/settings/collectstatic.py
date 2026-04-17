"""
Minimal settings module for running collectstatic during Docker build.

This settings file uses SQLite (no external dependencies) and doesn't require
any secrets to be present. It's used only during the Docker build phase to
collect static files into STATIC_ROOT.
"""

import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Minimal secret key for collectstatic (not used for anything security-sensitive)
SECRET_KEY = "collectstatic-build-key-not-for-production"

DEBUG = False

ALLOWED_HOSTS = ["*"]

# Minimal installed apps needed for collectstatic
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "accounts",
    "bots",
    "rest_framework",
    "concurrency",
    "allauth.socialaccount.providers.google",
    "drf_spectacular",
    "storages",
    "django_extensions",
    "anymail",
]

# Use SQLite for collectstatic (doesn't require external database)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Static files configuration
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATIC_URL = "static/"
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "static"),
]

# Templates (needed for some static files discovery)
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            os.path.join(BASE_DIR, "templates"),
            os.path.join(BASE_DIR, "accounts", "templates"),
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Required for allauth
SITE_ID = 1
AUTH_USER_MODEL = "accounts.User"

# Required settings
ROOT_URLCONF = "attendee.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# WhiteNoise for static files
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
]
