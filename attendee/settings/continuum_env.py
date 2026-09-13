"""Map Continuum's injected env var names onto the names attendee reads.

Continuum injects DATABASE_*, CACHE_DB_* and CLOUD_BUCKET_*; attendee reads
DATABASE_URL, REDIS_URL and AWS_*. This fills in os.environ before Django's
settings are imported, and never overwrites a value that is already set, so
local development, tests and CI are unaffected.
"""

import os
from urllib.parse import quote


def _set_default(name, value):
    if value and not os.environ.get(name):
        os.environ[name] = value


def _database_url():
    # Continuum gives DATABASE_HOST as "primary,replica" - take the primary.
    host = os.getenv("DATABASE_HOST", "").split(",")[0].strip()
    name, user = os.getenv("DATABASE_NAME"), os.getenv("DATABASE_USER")
    if not (host and name and user):
        return None
    auth = quote(user, safe="")
    password = os.getenv("DATABASE_PASSWORD", "")
    if password:
        auth += ":" + quote(password, safe="")
    port = os.getenv("DATABASE_PORT", "5432")
    return f"postgresql://{auth}@{host}:{port}/{quote(name, safe='')}"


def _redis_url():
    host = os.getenv("CACHE_DB_HOST")
    if not host:
        return None
    user, password = os.getenv("CACHE_DB_USER", ""), os.getenv("CACHE_DB_PASSWORD", "")
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if (user or password) else ""
    scheme = os.getenv("CACHE_DB_SCHEME", "redis")
    port, index = os.getenv("CACHE_DB_PORT", "6379"), os.getenv("CACHE_DB_INDEX", "0")
    # Deliberately no query string: attendee appends its own "?ssl_cert_reqs=..."
    return f"{scheme}://{auth}{host}:{port}/{index}"


def apply():
    _set_default("DATABASE_URL", _database_url())
    _set_default("REDIS_URL", _redis_url())
    _set_default("AWS_ACCESS_KEY_ID", os.getenv("CLOUD_BUCKET_ACCESS_KEY_ID"))
    _set_default("AWS_SECRET_ACCESS_KEY", os.getenv("CLOUD_BUCKET_ACCESS_KEY_SECRET"))
    _set_default("AWS_ENDPOINT_URL", os.getenv("CLOUD_BUCKET_ENDPOINT_URL"))
    _set_default("AWS_DEFAULT_REGION", os.getenv("CLOUD_BUCKET_REGION"))
    _set_default("AWS_RECORDING_STORAGE_BUCKET_NAME", os.getenv("CLOUD_BUCKET_NAME"))
