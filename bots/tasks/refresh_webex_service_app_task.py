import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from bots.models import WebexServiceApp
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError, refresh_service_app_tokens

logger = logging.getLogger(__name__)


def enqueue_refresh_webex_service_app_task(webex_service_app: WebexServiceApp):
    """Enqueue a token refresh for a Webex Service App."""

    # The scheduler uses tokens_refreshed_at to find the Service Apps to refresh, so set it now to not enqueue the task again in the next cycle
    with transaction.atomic():
        webex_service_app.tokens_refreshed_at = timezone.now()
        webex_service_app.save(update_fields=["tokens_refreshed_at"])
        refresh_webex_service_app.delay(webex_service_app.id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def refresh_webex_service_app(self, webex_service_app_id):
    """Celery task to refresh the tokens of a Webex Service App.

    A Webex refresh token expires 90 days after it was issued, unless it is used: using it to get an access token
    resets its lifetime to 90 days. Webex only does that once the token is a few days old, so the task needs to run
    periodically for the Service Apps of projects that rarely run bots.
    """
    logger.info(f"Refreshing tokens of Webex Service App {webex_service_app_id}")
    webex_service_app = WebexServiceApp.objects.get(id=webex_service_app_id)
    credentials = webex_service_app.get_credentials()

    try:
        tokens = refresh_service_app_tokens(webex_service_app.client_id, credentials["client_secret"], credentials["refresh_token"])
    except WebexAuthError as e:
        # The refresh token expired or was revoked. The Service App has to be authorized again.
        logger.error(f"Tokens of Webex Service App {webex_service_app_id} could not be refreshed: {e}")
        return

    # Webex has so far returned the same refresh token. If it ever issues a new one, the stored one may stop working.
    if tokens["refresh_token"] != credentials["refresh_token"]:
        logger.warning(f"Webex issued a new refresh token for Webex Service App {webex_service_app_id}, saving it")
        webex_service_app.set_credentials({**credentials, "refresh_token": tokens["refresh_token"]})
    logger.info(f"Successfully refreshed tokens of Webex Service App {webex_service_app_id}. The refresh token expires in {tokens['refresh_token_expires_in']} seconds")
