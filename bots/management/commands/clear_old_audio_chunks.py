import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from bots.models import AudioChunk

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Clears out audio chunks that are older than 1 day. Audio chunks are storing raw pcm audio in the database, so we don't want to keep them around for too long."

    def handle(self, *args, **options):
        expired_audio_chunks = AudioChunk.objects.exclude(audio_blob=b"").filter(created_at__lt=timezone.now() - timezone.timedelta(days=1))
        logger.info(f"Clearing out {expired_audio_chunks.count()} audio chunks")
        expired_audio_chunks.update(audio_blob=b"")
