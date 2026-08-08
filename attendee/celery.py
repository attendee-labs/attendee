import json
import os
import ssl

from celery import Celery

# Set the default Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "attendee.settings")

sslCertRequirements = None
if os.getenv("DISABLE_REDIS_SSL"):
    sslCertRequirements = ssl.CERT_NONE
elif os.getenv("REDIS_SSL_REQUIREMENTS"):
    if os.getenv("REDIS_SSL_REQUIREMENTS") == "none":
        sslCertRequirements = ssl.CERT_NONE
    elif os.getenv("REDIS_SSL_REQUIREMENTS") == "optional":
        sslCertRequirements = ssl.CERT_OPTIONAL
    elif os.getenv("REDIS_SSL_REQUIREMENTS") == "required":
        sslCertRequirements = ssl.CERT_REQUIRED

# Create the Celery app
if sslCertRequirements is not None:
    app = Celery(
        "attendee",
        broker_use_ssl={"ssl_cert_reqs": sslCertRequirements},
        redis_backend_use_ssl={"ssl_cert_reqs": sslCertRequirements},
    )
else:
    app = Celery("attendee")

# The Redis broker redelivers unacked tasks after visibility_timeout (default 3600s). With acks_late, run_bot stays
# unacked for the whole meeting, so this must exceed the longest bot runtime or a duplicate run_bot task gets
# delivered mid-meeting (issue #587).
broker_transport_options = {"visibility_timeout": int(os.getenv("CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS", 21600))}

# One use case for CELERY_BROKER_TRANSPORT_OPTIONS is to enable support for Redis Cluster hash
# tags. This is mainly to prevent CROSSSLOT errors when using Redis Cluster (https://github.com/celery/celery/issues/8276#issuecomment-3714489309)
# For this case set CELERY_BROKER_TRANSPORT_OPTIONS='{"global_keyprefix":"{celeryattendee}:","fanout_prefix":true,"fanout_patterns":true}'

if os.getenv("CELERY_BROKER_TRANSPORT_OPTIONS"):
    broker_transport_options.update(json.loads(os.getenv("CELERY_BROKER_TRANSPORT_OPTIONS")))

app.conf.update(broker_transport_options=broker_transport_options)

# Load configuration from Django settings
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks from all registered Django apps
app.autodiscover_tasks()
