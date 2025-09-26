# Azure Storage Settings
# These settings configure Azure Blob Storage integration for file uploads
# alongside the existing AWS S3 functionality.

import os

# Azure Storage Account Configuration
AZURE_STORAGE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
AZURE_STORAGE_CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER_NAME") 

# Authentication Methods (in order of preference):
# 1. Managed Identity (recommended for Azure-hosted applications)
#    - Only AZURE_STORAGE_ACCOUNT_NAME is needed
# 2. Connection String (for development/testing)
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
# 3. Account Key (least secure, not recommended for production)
AZURE_STORAGE_ACCOUNT_KEY = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")

# Storage Upload Configuration
# Controls which storage providers to use for file uploads
# Options: 's3' (AWS S3 only), 'azure' (Azure only), 'both' (upload to both)
STORAGE_UPLOAD_MODE = os.getenv("STORAGE_UPLOAD_MODE", "s3")

# Azure-specific storage backend (optional, for Django file storage)
if AZURE_STORAGE_ACCOUNT_NAME and AZURE_STORAGE_CONTAINER_NAME:
    AZURE_STORAGE_BACKEND = {
        "BACKEND": "storages.backends.azure_storage.AzureStorage",
        "OPTIONS": {
            "account_name": AZURE_STORAGE_ACCOUNT_NAME,
            "account_key": AZURE_STORAGE_ACCOUNT_KEY,
            "azure_container": AZURE_STORAGE_CONTAINER_NAME,
            "connection_string": AZURE_STORAGE_CONNECTION_STRING,
        },
    }
