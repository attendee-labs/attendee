# Azure Storage Retrieval Support

## Overview

This document describes the changes made to enable the application to **retrieve and display recordings from Azure Storage** when AWS S3 credentials are unavailable or when AWS S3 fails.

## Problem

Previously, when `STORAGE_UPLOAD_MODE=both`, recordings were uploaded to both AWS S3 and Azure Storage, but the application could only retrieve/display recordings from AWS S3. This caused failures when:
- AWS S3 credentials were missing or invalid
- AWS S3 service was unavailable
- User wanted to use Azure Storage as the primary retrieval source

## Solution

### 1. Database Schema Changes

**New Migration: `0059_add_azure_storage_urls.py`**

Added `azure_blob_url` field to both models:
- `Recording` model: Stores Azure Blob Storage URL for main recordings
- `BotDebugScreenshot` model: Stores Azure Blob Storage URL for debug recordings

```python
azure_blob_url = models.URLField(max_length=2048, null=True, blank=True)
```

### 2. Model URL Property Updates

**Modified `Recording.url` and `BotDebugScreenshot.url` properties:**

The `url` property now:
1. **Tries AWS S3 first**: Attempts to generate presigned URL from AWS S3
2. **Catches failures gracefully**: Logs warnings when AWS S3 fails
3. **Falls back to Azure**: Returns `azure_blob_url` if AWS S3 fails or is unavailable
4. **Returns None**: Only if both sources are unavailable

```python
@property
def url(self):
    if not self.file.name and not self.azure_blob_url:
        return None
    
    # Try to generate AWS S3 presigned URL first
    try:
        if self.file.name:
            return self.file.storage.bucket.meta.client.generate_presigned_url(...)
    except Exception as e:
        logger.warning(f"Failed to generate AWS S3 presigned URL: {e}")
    
    # Fallback to Azure Blob Storage URL
    if self.azure_blob_url:
        return self.azure_blob_url
    
    return None
```

### 3. Upload Code Updates

**AzureFileUploader Enhancement:**
- Added `get_blob_url()` method to retrieve the Azure blob URL

**UnifiedStorageManager Enhancement:**
- Stores reference to `azure_uploader` instance
- Added `get_azure_blob_url()` method to expose Azure URL

**BotController Upload Updates:**

**Main Recording (cleanup method):**
```python
# Get Azure blob URL after upload
azure_blob_url = storage_manager.get_azure_blob_url()
if azure_blob_url:
    self.save_azure_recording_url(azure_blob_url)
```

**Debug Recording (save_debug_recording method):**
```python
# Get Azure blob URL after upload
azure_blob_url = storage_manager.get_azure_blob_url()
if azure_blob_url:
    debug_screenshot.azure_blob_url = azure_blob_url
    debug_screenshot.save()
```

### 4. New Helper Methods

**`BotController.save_azure_recording_url(azure_blob_url)`:**
- Saves Azure blob URL to the main recording in the database
- Handles errors gracefully without failing the upload process

## Behavior Summary

### When STORAGE_UPLOAD_MODE=both

#### Upload Phase:
1. ✅ Recording uploads to AWS S3 (via Django storage)
2. ✅ Recording uploads to Azure Blob Storage (via UnifiedStorageManager)
3. ✅ Azure blob URL is saved to database (`azure_blob_url` field)

#### Retrieval Phase:
1. **AWS S3 Available**: Returns presigned AWS S3 URL
2. **AWS S3 Fails**: Returns Azure blob URL from database
3. **Both Fail**: Returns None

### When STORAGE_UPLOAD_MODE=azure

#### Upload Phase:
1. ❌ Recording does NOT upload to AWS S3
2. ✅ Recording uploads to Azure Blob Storage
3. ✅ Azure blob URL is saved to database

#### Retrieval Phase:
1. **AWS S3 check fails** (no file in S3): Falls back to Azure blob URL
2. **Returns**: Azure blob URL from database

### When STORAGE_UPLOAD_MODE=s3

#### Upload Phase:
1. ✅ Recording uploads to AWS S3 (via Django storage)
2. ❌ Recording does NOT upload to Azure
3. ❌ No Azure blob URL saved

#### Retrieval Phase:
1. **AWS S3 Available**: Returns presigned AWS S3 URL
2. **AWS S3 Fails**: No fallback available, returns None

## Files Modified

### 1. Database Migration
- `bots/migrations/0059_add_azure_storage_urls.py` - Added `azure_blob_url` fields

### 2. Models
- `bots/models.py`:
  - Updated `Recording` model with `azure_blob_url` field and fallback URL logic
  - Updated `BotDebugScreenshot` model with `azure_blob_url` field and fallback URL logic

### 3. Upload Components
- `bots/bot_controller/azure_file_uploader.py`:
  - Added `get_blob_url()` method

- `bots/bot_controller/unified_storage_manager.py`:
  - Added `azure_uploader` instance variable
  - Added `get_azure_blob_url()` method

- `bots/bot_controller/bot_controller.py`:
  - Added `save_azure_recording_url()` method
  - Updated `cleanup()` to save Azure URL after main recording upload
  - Updated `save_debug_recording()` to save Azure URL after debug recording upload

## Benefits

### ✅ High Availability
- Recordings remain accessible even when AWS S3 is down
- Automatic failover to Azure Storage

### ✅ Cost Optimization
- Can use Azure Storage as primary retrieval source
- Reduces AWS S3 data transfer costs if Azure is preferred

### ✅ Cloud Flexibility
- True multi-cloud storage with retrieval support
- Not locked into single cloud provider

### ✅ Backward Compatibility
- Existing recordings without `azure_blob_url` still work
- AWS S3 remains the primary retrieval method when available

## Deployment Steps

1. **Run Database Migration:**
   ```bash
   python manage.py migrate bots
   ```

2. **Rebuild Docker Image:**
   ```bash
   docker build -t attendeeacrdev.azurecr.io/attendeeacrdev:jp-azure-storage-v3 .
   docker push attendeeacrdev.azurecr.io/attendeeacrdev:jp-azure-storage-v3
   ```

3. **Update Kubernetes Deployment:**
   - Ensure `STORAGE_UPLOAD_MODE` environment variable is set correctly
   - Verify Azure Storage credentials are in `app-secrets`
   - Update `BOT_POD_IMAGE` and `CUBER_RELEASE_VERSION` env vars to use new image

4. **Test Scenarios:**
   - Test with `STORAGE_UPLOAD_MODE=both` and valid AWS + Azure credentials
   - Test with `STORAGE_UPLOAD_MODE=both` but invalid/missing AWS credentials
   - Test with `STORAGE_UPLOAD_MODE=azure` only
   - Verify recordings appear in UI in all scenarios

## Testing Checklist

- [ ] Migration runs successfully
- [ ] Recordings upload to both AWS S3 and Azure when `STORAGE_UPLOAD_MODE=both`
- [ ] Azure blob URL is saved to database
- [ ] Recordings display correctly when AWS S3 is available
- [ ] Recordings display correctly when AWS S3 credentials are removed
- [ ] Debug recordings display correctly with Azure fallback
- [ ] Existing recordings without Azure URLs still work

## Notes

- The Azure blob URL is a **public URL** if the container is public, or requires SAS token/authentication if private
- Consider implementing **Azure SAS token generation** for private containers in future iterations
- The current implementation uses the blob URL directly, which works for public containers or managed identity scenarios
