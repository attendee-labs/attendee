import logging
import mimetypes
import threading
from pathlib import Path

import rollbar

try:
    from google.cloud import storage

    GCS_AVAILABLE = True
except ImportError:
    storage = None
    GCS_AVAILABLE = False

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Explicit MIME type mapping for common recording formats
MIME_TYPE_MAP = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mhtml": "application/x-mimearchive",
    ".html": "text/html",
}


class GCSFileUploader:
    def __init__(
        self,
        bucket,
        filename,
        project_id=None,
        credentials_file=None,
    ):
        """
        Initialize the GCSFileUploader with a target bucket and blob name.

        Args:
            bucket (str): Google Cloud Storage bucket name.
            filename (str): Target blob name (path/key) inside the bucket.
            project_id (str, optional): GCP project ID.
            credentials_file (str, optional): Path to service account JSON credentials file.
                If not provided, uses Application Default Credentials.
        """
        if not GCS_AVAILABLE:
            raise ImportError(
                "google-cloud-storage is required for GCS support. "
                "Install it with: pip install google-cloud-storage"
            )

        if not bucket or not filename:
            raise ValueError("Both 'bucket' and 'filename' are required")

        # Initialize the storage client
        if credentials_file:
            self.storage_client = storage.Client.from_service_account_json(
                credentials_file,
                project=project_id,
            )
        else:
            # Use Application Default Credentials (ADC) or Workload Identity
            self.storage_client = storage.Client(project=project_id)

        self.bucket_name = bucket
        self.filename = filename
        self.bucket = self.storage_client.bucket(bucket)
        self._upload_thread = None

    def upload_file(self, file_path: str, callback=None):
        """Start an asynchronous upload of a file to Google Cloud Storage.

        Args:
            file_path (str): Path to the local file to upload.
            callback (callable, optional): Function to call when upload completes; receives True/False.
        """
        self._upload_thread = threading.Thread(target=self._upload_worker, args=(file_path, callback), daemon=True)
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        """Background thread that handles the actual file upload."""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                raise FileNotFoundError(f"File not found: {file_path}")

            file_size = file_path.stat().st_size
            logger.info(f"Starting GCS upload: {file_path} ({file_size} bytes) -> gs://{self.bucket_name}/{self.filename}")

            if file_size == 0:
                logger.warning(f"Warning: File {file_path} is empty (0 bytes)")
                rollbar.report_message(
                    message="GCS upload attempted with 0-byte file",
                    level="warning",
                    extra_data={
                        "file_path": str(file_path),
                        "bucket_name": self.bucket_name,
                        "blob_name": self.filename,
                        "context": "gcs_file_uploader",
                    },
                )

            # Determine content type based on file extension
            suffix = file_path.suffix.lower()
            content_type = MIME_TYPE_MAP.get(suffix)

            # Fallback to mimetypes library
            if not content_type:
                content_type, _ = mimetypes.guess_type(str(file_path))

            # Final fallback to binary stream (prevents wrong auto-detection)
            if not content_type:
                content_type = "application/octet-stream"
                logger.warning(f"Unknown file extension '{suffix}', using {content_type}")

            logger.info(f"Uploading with content_type={content_type} for file with suffix={suffix}")

            # Upload the file
            blob = self.bucket.blob(self.filename)
            blob.upload_from_filename(str(file_path), content_type=content_type)

            # Verify upload by checking blob size
            blob.reload()
            uploaded_size = blob.size
            logger.info(f"Successfully uploaded {file_path} to gs://{self.bucket_name}/{self.filename} (uploaded size: {uploaded_size} bytes)")

            if uploaded_size != file_size:
                logger.warning(f"Size mismatch! Local: {file_size} bytes, Uploaded: {uploaded_size} bytes")
                rollbar.report_message(
                    message="GCS upload size mismatch",
                    level="error",
                    extra_data={
                        "file_path": str(file_path),
                        "bucket_name": self.bucket_name,
                        "blob_name": self.filename,
                        "local_size": file_size,
                        "uploaded_size": uploaded_size,
                        "content_type": content_type,
                        "suffix": suffix,
                        "context": "gcs_file_uploader",
                    },
                )

            if callback:
                callback(True)

        except Exception as e:
            logger.error(f"Upload error: {e}")
            rollbar.report_exc_info(
                extra_data={
                    "file_path": str(file_path) if file_path else None,
                    "bucket_name": self.bucket_name,
                    "blob_name": self.filename,
                    "context": "gcs_file_uploader",
                },
            )
            if callback:
                callback(False)

    def wait_for_upload(self):
        """Wait for the current upload to complete."""
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()

    def delete_file(self, file_path: str):
        """Delete a file from the local filesystem (same behavior as the S3 version)."""
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
