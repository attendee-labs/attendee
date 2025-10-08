import logging
import threading
from pathlib import Path
from typing import Optional, Callable

try:
    from azure.storage.blob import BlobServiceClient
    from azure.identity import DefaultAzureCredential, EnvironmentCredential
    from azure.core.exceptions import AzureError
    AZURE_AVAILABLE = True
except ImportError:
    # Azure SDK not installed
    AZURE_AVAILABLE = False
    BlobServiceClient = None
    DefaultAzureCredential = None
    EnvironmentCredential = None
    AzureError = Exception

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class AzureFileUploader:
    """
    Azure Blob Storage file uploader with asynchronous upload capability.
    
    Supports both connection string and managed identity authentication methods.
    Follows Azure security best practices by preferring managed identity over connection strings.
    """
    
    def __init__(
        self, 
        container_name: str, 
        blob_name: str,
        storage_account_name: Optional[str] = None,
        connection_string: Optional[str] = None,
        account_key: Optional[str] = None
    ):
        """
        Initialize the Azure File Uploader.
        
        Args:
            container_name (str): The name of the Azure Storage container
            blob_name (str): The name of the blob to be stored
            storage_account_name (str, optional): Storage account name for managed identity auth
            connection_string (str, optional): Azure Storage connection string
            account_key (str, optional): Storage account key (not recommended for production)
        """
        if not AZURE_AVAILABLE:
            raise ImportError("Azure Storage SDK not installed. Install with: pip install azure-storage-blob azure-identity")
            
        self.container_name = container_name
        self.blob_name = blob_name
        self._upload_thread = None
        
        # Initialize blob service client with appropriate authentication
        self.blob_service_client = self._initialize_blob_service_client(
            storage_account_name, connection_string, account_key
        )
    
    def _initialize_blob_service_client(
        self, 
        storage_account_name: Optional[str], 
        connection_string: Optional[str], 
        account_key: Optional[str]
    ):
        """
        Initialize BlobServiceClient with the best available authentication method.
        
        Priority order:
        1. Managed Identity (recommended for Azure-hosted applications)
        2. Environment credentials (service principal)
        3. Connection string
        4. Storage account key (least secure)
        """
        try:
            if storage_account_name and not connection_string and not account_key:
                # Use managed identity - most secure for Azure-hosted applications
                logger.info("Using managed identity authentication for Azure Storage")
                credential = DefaultAzureCredential()
                account_url = f"https://{storage_account_name}.blob.core.windows.net"
                return BlobServiceClient(account_url=account_url, credential=credential)
            
            elif connection_string:
                # Use connection string - acceptable for development/testing
                logger.info("Using connection string authentication for Azure Storage")
                return BlobServiceClient.from_connection_string(connection_string)
            
            elif storage_account_name and account_key:
                # Use account key - not recommended for production
                logger.warning("Using account key authentication for Azure Storage - not recommended for production")
                account_url = f"https://{storage_account_name}.blob.core.windows.net"
                return BlobServiceClient(account_url=account_url, credential=account_key)
            
            else:
                raise ValueError("Must provide either storage_account_name (for managed identity), connection_string, or storage_account_name with account_key")
        
        except Exception as e:
            logger.error(f"Failed to initialize Azure Blob Service Client: {e}")
            raise
    
    def upload_file(self, file_path: str, callback: Optional[Callable[[bool], None]] = None):
        """
        Start an asynchronous upload of a file to Azure Blob Storage.
        
        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function to call when upload completes (receives success boolean)
        """
        self._upload_thread = threading.Thread(
            target=self._upload_worker, 
            args=(file_path, callback), 
            daemon=True
        )
        self._upload_thread.start()
    
    def _upload_worker(self, file_path: str, callback: Optional[Callable[[bool], None]] = None):
        """
        Background thread that handles the actual file upload.
        
        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function to call when upload completes
        """
        try:
            file_path_obj = Path(file_path)
            if not file_path_obj.exists():
                raise FileNotFoundError(f"File not found: {file_path}")
            
            # Get blob client for the specific blob
            blob_client = self.blob_service_client.get_blob_client(
                container=self.container_name, 
                blob=self.blob_name
            )
            
            # Upload the file with automatic chunking for large files
            with open(file_path_obj, 'rb') as data:
                blob_client.upload_blob(
                    data, 
                    overwrite=True,
                    # Enable parallel uploads for files > 100MB
                    max_concurrency=4
                )
            
            logger.info(f"Successfully uploaded {file_path} to Azure blob: {self.container_name}/{self.blob_name}")
            
            if callback:
                callback(True)
        
        except AzureError as e:
            logger.error(f"Azure Storage upload error: {e}")
            if callback:
                callback(False)
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
        file_path_obj = Path(file_path)
        if file_path_obj.exists():
            file_path_obj.unlink()
            logger.info(f"Deleted local file: {file_path}")
    
    def get_blob_url(self) -> str:
        """
        Get the blob URL for the uploaded file.
        
        Returns:
            str: The full URL to access the blob
        """
        blob_client = self.blob_service_client.get_blob_client(
            container=self.container_name, 
            blob=self.blob_name
        )
        return blob_client.url
