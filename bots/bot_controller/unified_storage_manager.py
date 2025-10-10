import logging
import os
from enum import Enum
from typing import Optional, List, Callable, Dict, Any

from .file_uploader import FileUploader
from .azure_file_uploader import AzureFileUploader, AZURE_AVAILABLE

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class StorageProvider(Enum):
    """Supported storage providers."""
    AWS_S3 = "s3"
    AZURE_BLOB = "azure"


class StorageConfig:
    """Configuration for a storage provider."""
    
    def __init__(self, provider: StorageProvider, config: Dict[str, Any]):
        self.provider = provider
        self.config = config
    
    def validate(self) -> bool:
        """Validate that required configuration is present."""
        if self.provider == StorageProvider.AWS_S3:
            return bool(self.config.get('bucket'))
        elif self.provider == StorageProvider.AZURE_BLOB:
            return bool(self.config.get('container_name'))
        return False


class UnifiedStorageManager:
    """
    Unified storage manager that supports multiple cloud storage providers.
    
    Features:
    - Upload to AWS S3, Azure Blob Storage, or both simultaneously
    - Configurable via environment variables
    - Asynchronous uploads with callback support
    - Automatic fallback and error handling
    """
    
    def __init__(self, file_key: str):
        """
        Initialize the unified storage manager.
        
        Args:
            file_key (str): The key/name for the file to be uploaded
        """
        self.file_key = file_key
        self.storage_configs = self._load_storage_configs()
        self.upload_threads = []
        self.azure_uploader = None  # Store reference to Azure uploader for URL retrieval
    
    def _load_storage_configs(self) -> List[StorageConfig]:
        """Load storage configurations from environment variables."""
        configs = []
        
        # Check for AWS S3 configuration
        if self._should_use_aws():
            s3_config = self._get_aws_config()
            if s3_config:
                configs.append(StorageConfig(StorageProvider.AWS_S3, s3_config))
        
        # Check for Azure Blob Storage configuration
        if self._should_use_azure():
            azure_config = self._get_azure_config()
            if azure_config:
                configs.append(StorageConfig(StorageProvider.AZURE_BLOB, azure_config))
        
        return [config for config in configs if config.validate()]
    
    def _should_use_aws(self) -> bool:
        """Check if AWS S3 upload is enabled."""
        upload_mode = os.getenv("STORAGE_UPLOAD_MODE", "s3").lower()
        return upload_mode in ["s3", "both", "all"]
    
    def _should_use_azure(self) -> bool:
        """Check if Azure Blob Storage upload is enabled."""
        upload_mode = os.getenv("STORAGE_UPLOAD_MODE", "s3").lower()
        return upload_mode in ["azure", "both", "all"] and AZURE_AVAILABLE
    
    def _get_aws_config(self) -> Optional[Dict[str, Any]]:
        """Get AWS S3 configuration from environment variables."""
        bucket = os.getenv("AWS_RECORDING_STORAGE_BUCKET_NAME")
        if not bucket:
            logger.warning("AWS bucket name not configured")
            return None
        
        return {
            'bucket': bucket,
            'endpoint_url': os.getenv("AWS_ENDPOINT_URL"),
            'region_name': os.getenv("AWS_DEFAULT_REGION"),
            'access_key_id': os.getenv("AWS_ACCESS_KEY_ID"),
            'secret_access_key': os.getenv("AWS_SECRET_ACCESS_KEY")
        }
    
    def _get_azure_config(self) -> Optional[Dict[str, Any]]:
        """Get Azure Blob Storage configuration from environment variables."""
        container_name = os.getenv("AZURE_STORAGE_CONTAINER_NAME")
        if not container_name:
            logger.warning("Azure container name not configured")
            return None
        
        # Support multiple authentication methods
        config = {
            'container_name': container_name,
            'storage_account_name': os.getenv("AZURE_STORAGE_ACCOUNT_NAME"),
            'connection_string': os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
            'account_key': os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
        }
        
        # Validate we have at least one authentication method
        if not any([
            config['storage_account_name'],
            config['connection_string'],
            config['account_key']
        ]):
            logger.warning("No Azure Storage authentication method configured")
            return None
        
        return config
    
    def upload_file(self, file_path: str, callback: Optional[Callable[[str, bool], None]] = None):
        """
        Upload file to configured storage providers.
        
        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function called for each upload completion
                                         receives (provider_name, success) parameters
        """
        if not self.storage_configs:
            logger.error("No storage providers configured")
            return
        
        successful_uploads = []
        failed_uploads = []
        
        def upload_callback(provider_name: str, success: bool):
            if success:
                successful_uploads.append(provider_name)
                logger.info(f"Successfully uploaded to {provider_name}")
            else:
                failed_uploads.append(provider_name)
                logger.error(f"Failed to upload to {provider_name}")
            
            if callback:
                callback(provider_name, success)
        
        # Start uploads for each configured provider
        for config in self.storage_configs:
            try:
                if config.provider == StorageProvider.AWS_S3:
                    self._upload_to_s3(config, file_path, upload_callback)
                elif config.provider == StorageProvider.AZURE_BLOB:
                    self._upload_to_azure(config, file_path, upload_callback)
            except Exception as e:
                logger.exception(f"Error starting upload to {config.provider.value}: {e}")
                upload_callback(config.provider.value, False)
    
    def _upload_to_s3(self, config: StorageConfig, file_path: str, callback: Callable[[str, bool], None]):
        """Upload file to AWS S3."""
        uploader = FileUploader(
            bucket=config.config['bucket'],
            key=self.file_key,
            endpoint_url=config.config.get('endpoint_url'),
            region_name=config.config.get('region_name'),
            access_key_id=config.config.get('access_key_id'),
            access_key_secret=config.config.get('secret_access_key')
        )
        
        def s3_callback(success: bool):
            callback("AWS S3", success)
        
        uploader.upload_file(file_path, s3_callback)
        self.upload_threads.append(uploader)
    
    def _upload_to_azure(self, config: StorageConfig, file_path: str, callback: Callable[[str, bool], None]):
        """Upload file to Azure Blob Storage."""
        uploader = AzureFileUploader(
            container_name=config.config['container_name'],
            blob_name=self.file_key,
            storage_account_name=config.config.get('storage_account_name'),
            connection_string=config.config.get('connection_string'),
            account_key=config.config.get('account_key')
        )
        
        # Store reference to Azure uploader for URL retrieval
        self.azure_uploader = uploader
        
        def azure_callback(success: bool):
            callback("Azure Blob Storage", success)
        
        uploader.upload_file(file_path, azure_callback)
        self.upload_threads.append(uploader)
    
    def wait_for_uploads(self):
        """Wait for all uploads to complete."""
        for uploader in self.upload_threads:
            try:
                uploader.wait_for_upload()
            except Exception as e:
                logger.exception(f"Error waiting for upload: {e}")
    
    def delete_local_file(self, file_path: str):
        """Delete the local file after upload."""
        if self.upload_threads:
            # Use the first uploader's delete method
            self.upload_threads[0].delete_file(file_path)
    
    @property
    def has_storage_configured(self) -> bool:
        """Check if any storage provider is configured."""
        return len(self.storage_configs) > 0
    
    @property
    def configured_providers(self) -> List[str]:
        """Get list of configured storage providers."""
        return [config.provider.value for config in self.storage_configs]
    
    def get_azure_blob_url(self) -> Optional[str]:
        """
        Get the Azure blob URL for the uploaded file.
        
        Returns:
            str: The Azure blob URL, or None if Azure upload was not configured
        """
        if self.azure_uploader:
            try:
                return self.azure_uploader.get_blob_url()
            except Exception as e:
                logger.error(f"Failed to get Azure blob URL: {e}")
                return None
        return None
