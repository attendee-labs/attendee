import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from bots.models import Credentials
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError, refresh_service_app_tokens

logger = logging.getLogger(__name__)


def enqueue_refresh_webex_service_app_credentials_task(credentials: Credentials):
    """Enqueue a token refresh for Webex Service App credentials."""

    # The scheduler uses updated_at as the time of the last refresh, so touch it now to not enqueue the task again in the next cycle
    with transaction.atomic():
        credentials.updated_at = timezone.now()
        credentials.save(update_fields=["updated_at"])
        refresh_webex_service_app_credentials.delay(credentials.id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def refresh_webex_service_app_credentials(self, credentials_id):
    """Celery task to refresh the token of Webex Service App credentials.

    A Webex refresh token expires 90 days after it was issued, unless it is used: using it to get an access token
    resets its lifetime to 90 days. Webex only does that once the token is a few days old, so the task needs to run
    periodically for the credentials of projects that rarely run bots.
    """
    logger.info(f"Refreshing Webex Service App credentials {credentials_id}")
    credentials = Credentials.objects.get(id=credentials_id)
    credentials_data = credentials.get_credentials()

    try:
        tokens = refresh_service_app_tokens(credentials_data["client_id"], credentials_data["client_secret"], credentials_data["refresh_token"])
    except WebexAuthError as e:
        # The refresh token expired or was revoked. A new one has to be issued and saved in the credentials.
        logger.error(f"Webex Service App credentials {credentials_id} could not be refreshed: {e}")
        return

    # Save the refresh token Webex returned, in case it is a new one
    credentials.set_credentials({**credentials_data, "refresh_token": tokens["refresh_token"]})
    logger.info(f"Successfully refreshed Webex Service App credentials {credentials_id}. The refresh token expires in {tokens['refresh_token_expires_in']} seconds")
