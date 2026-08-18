import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import RecorderUpload, RecorderUploadStates
from bots.recorder_sessions_api_utils import abort_recorder_session
from bots.recorder_upload_storage import session_abandon_ttl_minutes

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Expires desktop recorder sessions whose upload was abandoned (no activity past the TTL) and aborts their orphaned S3 multipart uploads."

    def handle(self, *args, **options):
        ttl_minutes = session_abandon_ttl_minutes()
        cutoff = timezone.now() - timezone.timedelta(minutes=ttl_minutes)

        abandoned = RecorderUpload.objects.filter(
            state__in=[RecorderUploadStates.CREATED, RecorderUploadStates.UPLOADING, RecorderUploadStates.UPLOADED],
            last_activity_at__lt=cutoff,
        ).select_related("bot")

        count = abandoned.count()
        logger.info(f"Found {count} abandoned recorder sessions (idle > {ttl_minutes} min)")

        for recorder_upload in abandoned:
            try:
                abort_recorder_session(recorder_upload)
                logger.info(f"Expired abandoned recorder session {recorder_upload.object_id} (bot {recorder_upload.bot.object_id})")
            except Exception as e:
                logger.error(f"Failed to expire recorder session {recorder_upload.object_id}: {e}")
