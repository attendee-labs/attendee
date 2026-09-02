import logging

from django.db import transaction
from django.utils import timezone

from bots.models import ZoomOAuthConnection, ZoomOAuthConnectionStates
from bots.redis_utils import zoom_oauth_connection_token_lock
from bots.zoom_oauth_connections_utils import ZoomAPIAuthenticationError, _get_access_token, _get_zoom_meetings, _get_zoom_personal_meeting_id, _handle_zoom_api_authentication_error, _upsert_zoom_meeting_to_zoom_oauth_connection_mapping

logger = logging.getLogger(__name__)

from celery import shared_task


def enqueue_sync_zoom_oauth_connection_task(zoom_oauth_connection: ZoomOAuthConnection):
    """Enqueue a sync zoom oauth connection task for a zoom oauth connection."""
    if not zoom_oauth_connection.is_local_recording_token_supported:
        logger.info(f"Skipping sync zoom oauth connection task for {zoom_oauth_connection.id} because it does not support local recording tokens")
        return

    with transaction.atomic():
        zoom_oauth_connection.sync_task_enqueued_at = timezone.now()
        zoom_oauth_connection.sync_task_requested_at = None
        zoom_oauth_connection.save()
        sync_zoom_oauth_connection.delay(zoom_oauth_connection.id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def sync_zoom_oauth_connection(self, zoom_oauth_connection_id):
    """Celery task to sync zoom meetings with a zoom oauth connection."""
    logger.info(f"Syncing zoom oauth connection {zoom_oauth_connection_id}")
    zoom_oauth_connection = ZoomOAuthConnection.objects.get(id=zoom_oauth_connection_id)

    try:
        # Set the sync start time
        sync_started_at = timezone.now()

        # Token refresh is serialized via zoom_oauth_connection_token_lock inside
        # _get_access_token (shared with refresh/join) so concurrent sync cannot
        # rotate credentials out from under another worker.
        with zoom_oauth_connection_token_lock(zoom_oauth_connection_id):
            access_token = _get_access_token(zoom_oauth_connection)

        zoom_meetings = _get_zoom_meetings(access_token)

        logger.info(f"Fetched {len(zoom_meetings)} meetings from Zoom for zoom oauth connection {zoom_oauth_connection_id}")

        zoom_personal_meeting_id = _get_zoom_personal_meeting_id(access_token)
        zoom_meeting_ids = [zoom_meeting["id"] for zoom_meeting in zoom_meetings] + [zoom_personal_meeting_id]

        _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(zoom_meeting_ids, zoom_oauth_connection)

        # Update zoom oauth connection sync success timestamp and window
        zoom_oauth_connection.last_attempted_sync_at = timezone.now()
        zoom_oauth_connection.last_successful_sync_at = zoom_oauth_connection.last_attempted_sync_at
        zoom_oauth_connection.last_successful_sync_started_at = sync_started_at
        zoom_oauth_connection.state = ZoomOAuthConnectionStates.CONNECTED
        zoom_oauth_connection.connection_failure_data = None
        zoom_oauth_connection.save()

    except ZoomAPIAuthenticationError as e:
        _handle_zoom_api_authentication_error(zoom_oauth_connection, e)

    except Exception as e:
        logger.exception(f"Zoom OAuth connection sync failed with {type(e).__name__} for {zoom_oauth_connection_id}: {e}")
        zoom_oauth_connection.last_attempted_sync_at = timezone.now()
        zoom_oauth_connection.save()
        raise
