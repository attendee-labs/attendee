"""
Swift-based file uploader for recording files
"""
import logging
import threading
from pathlib import Path

from bots.storage.swift_utils import upload_file_to_swift, delete_file_from_swift

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class SwiftFileUploader:
    def __init__(self, container, key):
        """Initialize the SwiftFileUploader with a container name.
        
        Args:
            container (str): The name of the Swift container to upload to
            key (str): The name of the to be stored file
        """
        self.container = container
        self.key = key
        self._upload_thread = None

    def upload_file(self, file_path: str, callback=None):
        """Start an asynchronous upload of a file to Swift.
        
        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function to call when upload completes
        """
        self._upload_thread = threading.Thread(
            target=self._upload_worker, 
            args=(file_path, callback), 
            daemon=True
        )
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
        try:
            if file_path.exists():
                file_path.unlink()
                logger.info(f"Deleted local file: {file_path}")
            else:
                logger.warning(f"Local file not found: {file_path}")
        except Exception as e:
            logger.error(f"Failed to delete local file {file_path}: {e}")
            raise