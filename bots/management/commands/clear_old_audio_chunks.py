import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import AudioChunk

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Clears out audio chunks that are older than 1 day. Audio chunks are storing raw pcm audio in the database, so we don't want to keep them around for too long. If audio chunks were stored remotely, this only deletes the file url, not the remote data."

    def handle(self, *args, **options):
        expired_audio_chunks = AudioChunk.objects.exclude(audio_blob=b"", audio_blob_remote_file=None).filter(created_at__lt=timezone.now() - timezone.timedelta(days=1))
        logger.info(f"Clearing out {expired_audio_chunks.count()} audio chunks")
        # Note that this clears out the remote audio file column, but does not delete the remote file.
        # You need to set your bucket params for that.
        expired_audio_chunks.update(audio_blob=b"", audio_blob_remote_file=None)
