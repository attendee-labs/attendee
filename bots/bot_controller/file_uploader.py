import logging
import threading
from pathlib import Path

from bots.storage import get_container_name, get_swift_client, upload_file_to_swift

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class FileUploader:
    def __init__(self, bucket, key):
        """Initialize the FileUploader with a Swift container name.

        Args:
            bucket (str): The name of the Swift container (kept for compatibility)
            key (str): The name of the to be stored file
        """
        self.swift_client = get_swift_client()
        self.container = get_container_name()  # Use the configured container name
        self.key = key
        self._upload_thread = None

    def upload_file(self, file_path: str, callback=None):
        """Start an asynchronous upload of a file to Swift.

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

            # Upload the file using Swift
            upload_file_to_swift(str(file_path), self.key)

            logger.info(f"Successfully uploaded {file_path} to swift://{self.container}/{self.key}")

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
