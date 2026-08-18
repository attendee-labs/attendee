"""
S3 multipart-upload helpers for the desktop recorder SDK.

The desktop recorder is an untrusted client that cannot hold AWS credentials, so instead
of uploading directly (as the trusted bot pod does via boto3 in
`bots/bot_controller/s3_file_uploader.py`), it uploads via presigned S3 multipart URLs
issued by these helpers. The media lands in the same "recordings" storage the bot uses,
so `Recording.file` / download works unchanged.

Only the S3 storage protocol is supported in v1. `recorder_uploads_supported()` guards
the endpoints so Azure deployments get a clear error instead of a crash.
"""

import logging
import os

from django.conf import settings
from django.core.files.storage import storages

logger = logging.getLogger(__name__)


# --- Configuration (env vars) ---------------------------------------------------------


def upload_url_expiry_seconds():
    return int(os.getenv("RECORDER_UPLOAD_URL_EXPIRY_SECONDS", 3600))


def multipart_part_size_bytes():
    return int(os.getenv("RECORDER_MULTIPART_PART_SIZE_BYTES", 8 * 1024 * 1024))


def session_abandon_ttl_minutes():
    return int(os.getenv("RECORDER_SESSION_ABANDON_TTL_MINUTES", 120))


def max_upload_bytes():
    return int(os.getenv("RECORDER_MAX_UPLOAD_BYTES", 5 * 1024 * 1024 * 1024))


def recorder_uploads_supported():
    return settings.STORAGE_PROTOCOL == "s3"


# --- S3 access ------------------------------------------------------------------------


def _recordings_storage():
    return storages["recordings"]


def _client_and_bucket():
    storage = _recordings_storage()
    return storage.bucket.meta.client, storage.bucket_name


# --- Multipart operations -------------------------------------------------------------


def initiate_multipart_upload(s3_key, content_type):
    client, bucket = _client_and_bucket()
    response = client.create_multipart_upload(Bucket=bucket, Key=s3_key, ContentType=content_type)
    return response["UploadId"]


def generate_part_upload_urls(s3_key, upload_id, part_numbers):
    """Return [{part_number, url}] presigned PUT URLs for the given part numbers."""
    client, bucket = _client_and_bucket()
    expiry = upload_url_expiry_seconds()
    urls = []
    for part_number in part_numbers:
        url = client.generate_presigned_url(
            "upload_part",
            Params={"Bucket": bucket, "Key": s3_key, "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=expiry,
        )
        urls.append({"part_number": part_number, "url": url})
    return urls


def list_uploaded_parts(s3_key, upload_id):
    """Return [{part_number, etag, size}] for parts S3 has actually received (for resume)."""
    client, bucket = _client_and_bucket()
    parts = []
    marker = 0
    while True:
        response = client.list_parts(Bucket=bucket, Key=s3_key, UploadId=upload_id, PartNumberMarker=marker)
        for part in response.get("Parts", []):
            parts.append({"part_number": part["PartNumber"], "etag": part["ETag"], "size": part["Size"]})
        if not response.get("IsTruncated"):
            break
        marker = response["NextPartNumberMarker"]
    return parts


def complete_multipart_upload(s3_key, upload_id, parts):
    """Finalize the multipart upload. `parts` is [{part_number, etag}]."""
    client, bucket = _client_and_bucket()
    multipart = {"Parts": [{"ETag": p["etag"], "PartNumber": p["part_number"]} for p in sorted(parts, key=lambda p: p["part_number"])]}
    client.complete_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id, MultipartUpload=multipart)


def abort_multipart_upload(s3_key, upload_id):
    """Best-effort abort so orphaned parts stop incurring storage cost. Never raises."""
    try:
        client, bucket = _client_and_bucket()
        client.abort_multipart_upload(Bucket=bucket, Key=s3_key, UploadId=upload_id)
    except Exception as e:
        logger.warning(f"Failed to abort multipart upload for key={s3_key} upload_id={upload_id}: {e}")


def object_size(s3_key):
    """Size in bytes of the finalized object, or None if it does not exist."""
    try:
        client, bucket = _client_and_bucket()
        response = client.head_object(Bucket=bucket, Key=s3_key)
        return response["ContentLength"]
    except Exception as e:
        logger.warning(f"Failed to head object key={s3_key}: {e}")
        return None
