import os
from urllib.parse import quote

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

POSTGRES_ENV_VARS = ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER")


def _database_url_from_postgres_env():
    # DATABASE_URL takes precedence; these vars are only consulted when it's unset.
    host = os.getenv("POSTGRES_HOST")
    name = os.getenv("POSTGRES_DB")
    user = os.getenv("POSTGRES_USER")
    if not (host and name and user):
        return None
    password = os.getenv("POSTGRES_PASSWORD", "")
    port = os.getenv("POSTGRES_PORT", "5432")
    auth = quote(user, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    return f"postgresql://{auth}@{host}:{port}/{quote(name, safe='')}"


def default_database(ssl_require):
    url = os.getenv("DATABASE_URL") or _database_url_from_postgres_env()
    if not url:
        raise ImproperlyConfigured("Database is not configured. Set DATABASE_URL, or set " + ", ".join(POSTGRES_ENV_VARS) + " (and optionally POSTGRES_PASSWORD, POSTGRES_PORT).")
    return dj_database_url.parse(
        url,
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=ssl_require,
        # Set to "true" behind a transaction-pooling pooler (PgBouncer, etc.).
        disable_server_side_cursors=os.getenv("DISABLE_SERVER_SIDE_CURSORS", "false") == "true",
    )
