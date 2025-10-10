import logging
import threading
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class S3FileUploader:
    def __init__(self, bucket, filename, endpoint_url=None, region_name=None, access_key_id=None, access_key_secret=None):
        """Initialize the S3FileUploader with an S3 bucket name.

        Args:
            bucket (str): The name of the S3 bucket to upload to
            filename (str): The name of the to be stored file
        """
        self.s3_client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name, aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret)
        self.bucket = bucket
        self.filename = filename
        self._upload_thread = None

    def upload_file(self, file_path: str, callback=None):
        """Start an asynchronous upload of a file to S3.

        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function to call when upload completes
        """
        self._upload_thread = threading.Thread(target=self._upload_worker, args=(file_path, callback), daemon=True)
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        """Background thread that handles the actual file upload.

        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function to call when upload completes
        """
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                raise FileNotFoundError(f"File not found: {file_path}")

            # Upload the file using S3's multipart upload functionality
            self.s3_client.upload_file(str(file_path), self.bucket, self.filename)

            logger.info(f"Successfully uploaded {file_path} to s3://{self.bucket}/{self.filename}")

            if callback:
                callback(True)

        except Exception as e:
            logger.error(f"Upload error: {e}")
            if callback:
                callback(False)

    def wait_for_upload(self):
        """Wait for the current upload to complete."""
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()

    def delete_file(self, file_path: str):
        """Delete a file from the local filesystem."""
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
