"""
Azure Storage integration updates for bot_controller.py

This file contains the modifications made to integrate Azure Storage support
alongside the existing AWS S3 functionality in the bot controller.

Key changes made:

1. Added import for UnifiedStorageManager
2. Updated cleanup() method to use unified storage approach
3. Added fallback to legacy AWS S3 upload if no storage providers are configured
4. Added comprehensive logging for upload operations

The unified approach allows:
- AWS S3 only uploads (legacy behavior)  
- Azure Storage only uploads
- Simultaneous uploads to both providers
- Configuration via environment variables
"""

# Key environment variables for controlling upload behavior:
# STORAGE_UPLOAD_MODE - Controls which providers to use:
#   "s3" (default) - AWS S3 only
#   "azure" - Azure Storage only  
#   "both" - Upload to both simultaneously

# AWS S3 Configuration (existing):
# AWS_RECORDING_STORAGE_BUCKET_NAME
# AWS_ACCESS_KEY_ID
# AWS_SECRET_ACCESS_KEY
# AWS_ENDPOINT_URL
# AWS_DEFAULT_REGION

# Azure Storage Configuration (new):
# AZURE_STORAGE_ACCOUNT_NAME
# AZURE_STORAGE_CONTAINER_NAME
# AZURE_STORAGE_CONNECTION_STRING (optional)
# AZURE_STORAGE_ACCOUNT_KEY (optional, not recommended for production)

# Example environment configuration for dual uploads:
"""
# Enable uploads to both AWS S3 and Azure Storage
STORAGE_UPLOAD_MODE=both

# AWS Configuration
AWS_RECORDING_STORAGE_BUCKET_NAME=my-recordings-bucket
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key

# Azure Configuration (using managed identity - recommended)
AZURE_STORAGE_ACCOUNT_NAME=myrecordingsstorage
AZURE_STORAGE_CONTAINER_NAME=recordings

# Or Azure with connection string (for development)
# AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
"""
