import logging
import time

from botocore.exceptions import ClientError, EndpointConnectionError
from django.conf import settings
from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Creates the recording and audio chunk buckets on the S3-compatible storage server if they don't exist yet. Waits for the server to be reachable."

    def add_arguments(self, parser):
        parser.add_argument("--max-attempts", type=int, default=30, help="How many times to retry while the storage server is unreachable (1 second apart)")

    def handle(self, *args, **options):
        if settings.STORAGE_PROTOCOL == "azure":
            logger.info("Storage protocol is azure, no buckets to create")
            return

        # Buckets are private by default, so no access policy needs to be set
        for alias in ["recordings", "audio_chunks"]:
            storage = storages[alias]
            if not storage.bucket_name:
                raise CommandError(f"No bucket name configured for the '{alias}' storage. Set MINIO_RECORDING_STORAGE_BUCKET_NAME.")
            self._create_bucket_if_missing(storage.connection.meta.client, storage.bucket_name, options["max_attempts"])

    def _create_bucket_if_missing(self, client, bucket_name, max_attempts):
        for attempt in range(1, max_attempts + 1):
            try:
                client.head_bucket(Bucket=bucket_name)
                logger.info(f"Bucket {bucket_name} already exists")
                return
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
                    raise
                client.create_bucket(Bucket=bucket_name)
                logger.info(f"Created bucket {bucket_name}")
                return
            except EndpointConnectionError:
                logger.info(f"Storage server not reachable yet (attempt {attempt}/{max_attempts})")
                time.sleep(1)

        raise CommandError(f"Storage server was not reachable after {max_attempts} attempts")
