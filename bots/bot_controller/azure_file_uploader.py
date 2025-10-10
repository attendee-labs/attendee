import logging
import threading
from pathlib import Path

from azure.storage.blob import BlobClient, BlobServiceClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class AzureFileUploader:
    def __init__(
        self,
        container,
        filename,
        connection_string,
        account_key,
        account_name,
    ):
        """
        Initialize the AzureFileUploader with a target container and blob name.

        Args:
            container (str): Azure Blob Storage container name.
            filename (str): Target blob name (path/key) inside the container.
            connection_string (str, optional): Full Azure Storage connection string.
            account_key (str, optional): Account key (used if no connection string).
            account_name (str, optional): Account name (used if no connection string).
        """
        if not container or not filename:
            raise ValueError("Both 'container' and 'filename' are required")

        # Prefer connection string if provided; otherwise fall back to account_name + account_key
        if connection_string:
            service_client = BlobServiceClient.from_connection_string(connection_string)
        else:
            if not (account_name and account_key):
                raise ValueError("Provide either AZURE_CONNECTION_STRING or both AZURE_ACCOUNT_NAME and AZURE_ACCOUNT_KEY")
            account_url = f"https://{account_name}.blob.core.windows.net"
            service_client = BlobServiceClient(account_url=account_url, credential=account_key)

        # Keep a BlobClient ready to use (mirrors S3 "bucket/key" pairing)
        self.container = container
        self.filename = filename
        self.blob_client: BlobClient = service_client.get_blob_client(container=container, blob=filename)

        self._upload_thread = None

    def upload_file(self, file_path: str, callback=None):
        """Start an asynchronous upload of a file to Azure Blob Storage.

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

            # Upload the file; let the SDK handle chunking under the hood.
            with file_path.open("rb") as f:
                # overwrite=True to mirror typical "upsert" behavior similar to S3 put
                self.blob_client.upload_blob(f, overwrite=True)

            account_url = self.blob_client.url.split(f"/{self.container}/")[0]
            logger.info(f"Successfully uploaded {file_path} to {account_url}/{self.container}/{self.filename}")

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
        """Delete a file from the local filesystem (same behavior as the S3 version)."""
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
