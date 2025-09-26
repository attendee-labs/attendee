# Azure Storage Integration Migration Guide

This guide explains how to integrate Azure Storage alongside your existing AWS S3 storage in the Attendee Bot application.

## Overview

The integration adds support for Azure Blob Storage while maintaining full backward compatibility with existing AWS S3 functionality. You can:

- Continue using AWS S3 only (default behavior)
- Switch to Azure Storage only
- Upload to both AWS S3 and Azure Storage simultaneously
- Configure behavior via environment variables

## Features Added

### 1. Azure File Uploader (`azure_file_uploader.py`)
- Asynchronous Azure Blob Storage uploads
- Multiple authentication methods (Managed Identity, Connection String, Account Key)
- Follows Azure security best practices
- Automatic chunking for large files
- Comprehensive error handling and logging

### 2. Unified Storage Manager (`unified_storage_manager.py`)
- Manages uploads to multiple storage providers
- Configurable via environment variables
- Simultaneous uploads with individual success/failure tracking
- Graceful fallback to legacy behavior

### 3. Azure Storage Settings (`azure_storage_settings.py`)
- Centralized Azure configuration
- Django settings integration
- Environment-based configuration

## Installation

1. **Install Azure SDK dependencies:**
   ```bash
   pip install -r requirements_azure.txt
   ```

2. **Update main requirements (optional):**
   ```bash
   # Add Azure dependencies to main requirements.txt
   cat requirements_azure.txt >> requirements.txt
   ```

## Configuration

### Environment Variables

#### Core Configuration
```bash
# Upload behavior control
STORAGE_UPLOAD_MODE=both  # Options: s3, azure, both

# Azure Storage Account
AZURE_STORAGE_ACCOUNT_NAME=your_storage_account
AZURE_STORAGE_CONTAINER_NAME=recordings
```

#### Authentication Methods (choose one)

**Option 1: Managed Identity (Recommended for Azure-hosted apps)**
```bash
# Only account name needed - uses Azure Managed Identity
AZURE_STORAGE_ACCOUNT_NAME=your_storage_account
```

**Option 2: Connection String (Good for development)**
```bash
AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
```

**Option 3: Account Key (Not recommended for production)**
```bash
AZURE_STORAGE_ACCOUNT_NAME=your_storage_account
AZURE_STORAGE_ACCOUNT_KEY=your_account_key
```

### AWS S3 Configuration (existing)
```bash
AWS_RECORDING_STORAGE_BUCKET_NAME=your-bucket
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_ENDPOINT_URL=https://s3.amazonaws.com  # Optional
AWS_DEFAULT_REGION=us-east-1  # Optional
```

## Usage Examples

### Example 1: AWS S3 Only (Default/Legacy)
```bash
# No changes needed - existing behavior
STORAGE_UPLOAD_MODE=s3  # or omit (defaults to s3)
AWS_RECORDING_STORAGE_BUCKET_NAME=my-recordings
```

### Example 2: Azure Storage Only
```bash
STORAGE_UPLOAD_MODE=azure
AZURE_STORAGE_ACCOUNT_NAME=myrecordingsstorage
AZURE_STORAGE_CONTAINER_NAME=recordings
```

### Example 3: Both AWS S3 and Azure Storage
```bash
STORAGE_UPLOAD_MODE=both

# AWS Configuration
AWS_RECORDING_STORAGE_BUCKET_NAME=my-recordings
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=xxx...

# Azure Configuration  
AZURE_STORAGE_ACCOUNT_NAME=myrecordingsstorage
AZURE_STORAGE_CONTAINER_NAME=recordings
```

## Azure Storage Setup

### 1. Create Storage Account
```bash
# Using Azure CLI
az storage account create \
  --name myrecordingsstorage \
  --resource-group my-resource-group \
  --location eastus \
  --sku Standard_LRS
```

### 2. Create Container
```bash
az storage container create \
  --name recordings \
  --account-name myrecordingsstorage
```

### 3. Configure Access

#### For Managed Identity (Recommended)
```bash
# Assign Storage Blob Data Contributor role to your app
az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee <your-app-principal-id> \
  --scope /subscriptions/<subscription>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/myrecordingsstorage
```

#### For Connection String
```bash
# Get connection string
az storage account show-connection-string \
  --name myrecordingsstorage \
  --resource-group my-resource-group
```

## Security Best Practices

1. **Use Managed Identity** when running on Azure (App Service, Container Instances, etc.)
2. **Avoid Account Keys** in production - use connection strings or managed identity
3. **Rotate credentials regularly** if using keys or connection strings
4. **Use RBAC** for fine-grained access control
5. **Enable storage account encryption** and secure transfer

## Monitoring and Logging

The integration provides comprehensive logging:

```python
# Example log output
INFO: Using managed identity authentication for Azure Storage
INFO: Uploading to configured providers: AWS S3, Azure Blob Storage  
INFO: Successfully uploaded recording to AWS S3
INFO: Successfully uploaded recording to Azure Blob Storage
INFO: Unified storage manager finished uploading to all providers
```

## Troubleshooting

### Common Issues

1. **Azure SDK Import Error**
   ```
   ImportError: No module named 'azure.storage.blob'
   ```
   **Solution:** Install Azure dependencies: `pip install -r requirements_azure.txt`

2. **Authentication Failed**
   ```
   DefaultAzureCredential failed to retrieve a token
   ```
   **Solution:** Ensure managed identity is configured or use connection string

3. **Container Not Found**
   ```
   The specified container does not exist
   ```
   **Solution:** Create the container first or check container name

4. **Permission Denied**
   ```
   AuthorizationPermissionMismatch: This request is not authorized
   ```
   **Solution:** Assign proper RBAC roles (Storage Blob Data Contributor)

### Debug Mode

Enable debug logging:
```python
import logging
logging.getLogger('azure.storage').setLevel(logging.DEBUG)
```

## Migration Steps

### For Existing Applications

1. **Install dependencies:** `pip install -r requirements_azure.txt`
2. **Test with Azure only:** Set `STORAGE_UPLOAD_MODE=azure`
3. **Verify uploads work** to Azure Storage
4. **Enable dual uploads:** Set `STORAGE_UPLOAD_MODE=both`
5. **Monitor logs** to ensure both uploads succeed
6. **Adjust as needed** based on your requirements

### Rollback Plan

To rollback to AWS S3 only:
```bash
# Remove or comment out Azure variables
# AZURE_STORAGE_ACCOUNT_NAME=
# AZURE_STORAGE_CONTAINER_NAME=

# Ensure AWS-only mode
STORAGE_UPLOAD_MODE=s3
```

## Performance Considerations

- **Parallel uploads** to multiple providers may increase upload time slightly
- **Large files** (>100MB) use automatic chunking for better performance  
- **Network bandwidth** usage increases with dual uploads
- Consider **geographic proximity** of storage accounts to your application

## Cost Implications

- **Dual uploads** will approximately double your storage costs
- **Azure pricing** may differ from AWS - compare costs for your usage patterns
- **Egress charges** may apply when downloading from either provider
- Consider **storage tiers** (Hot/Cool/Archive) for cost optimization

## Support

For issues specific to this integration:
1. Check application logs for detailed error messages
2. Verify environment variable configuration
3. Test Azure CLI connectivity: `az storage account list`
4. Review Azure Storage metrics in Azure Portal
