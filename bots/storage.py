import logging

from django.core.files.storage import Storage, storages
from django.utils.deconstruct import deconstructible

logger = logging.getLogger(__name__)
import requests
from django.conf import settings


@deconstructible
class StorageAlias(Storage):
    def __init__(self, alias: str):
        self.alias = alias

    @property
    def _wrapped(self):
        return storages[self.alias]

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    # Methods defined in base Storage class need explicit delegation
    # since __getattr__ is only called when the attribute isn't found in the MRO
    def delete(self, name):
        return self._wrapped.delete(name)

    def url(self, name):
        return self._wrapped.url(name)


def download_blob_from_remote_storage(url: str, max_retries: int) -> memoryview:
    for attempt in range(max_retries):
        try:
            response = requests.get(url)
        except requests.exceptions.RequestException:
            logger.warning(
                "download_blob_from_remote_storage %s: request failed (attempt %d/%d)",
                url,
                attempt + 1,
                max_retries,
            )
            continue

        if not response.ok:
            logger.warning(
                "download_blob_from_remote_storage %s: HTTP %s %s (attempt %d/%d)",
                url,
                response.status_code,
                response.reason,
                attempt + 1,
                max_retries,
            )
            continue

        if not response.content:
            logger.warning(
                "download_blob_from_remote_storage %s: empty response (attempt %d/%d)",
                url,
                attempt + 1,
                max_retries,
            )
            continue

        return memoryview(response.content)

    logger.warning("download_blob_from_remote_storage %s: all %d attempts failed", url, max_retries)
    return memoryview(b"")


def remote_storage_url(file_field):
    if settings.STORAGE_PROTOCOL == "azure":
        return file_field.url

    # Generate a temporary signed URL that expires in 30 minutes (1800 seconds)
    return file_field.storage.bucket.meta.client.generate_presigned_url(
        "get_object",
        Params={"Bucket": file_field.storage.bucket_name, "Key": file_field.name},
        ExpiresIn=1800,
    )
