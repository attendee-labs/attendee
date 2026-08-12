# MinIO Storage Setup Guide

This guide explains how to configure Attendee to use MinIO for object storage.

## Overview

MinIO is an open-source, S3-compatible object storage server. Attendee now supports MinIO as a storage backend alongside AWS S3 and Azure Blob Storage.

## Configuration

To use MinIO as your storage backend, you need to set the following environment variables:

### Required Variables

1. **STORAGE_PROTOCOL**: Set to `"minio"` to enable MinIO storage
   ```bash
   export STORAGE_PROTOCOL=minio
   ```

2. **MINIO_ENDPOINT_URL**: The URL of your MinIO server
   ```bash
   export MINIO_ENDPOINT_URL=http://minio-server:9000
   ```

3. **MINIO_ACCESS_KEY**: Your MinIO access key (similar to AWS_ACCESS_KEY_ID)
   ```bash
   export MINIO_ACCESS_KEY=your-minio-access-key
   ```

4. **MINIO_SECRET_KEY**: Your MinIO secret key (similar to AWS_SECRET_ACCESS_KEY)
   ```bash
   export MINIO_SECRET_KEY=your-minio-secret-key
   ```

5. **MINIO_RECORDING_STORAGE_BUCKET_NAME**: The bucket name for storing recordings
   ```bash
   export MINIO_RECORDING_STORAGE_BUCKET_NAME=attendee-recordings
   ```

### Optional Variables

1. **MINIO_AUDIO_CHUNK_STORAGE_BUCKET_NAME**: Separate bucket for audio chunks (defaults to MINIO_RECORDING_STORAGE_BUCKET_NAME)
   ```bash
   export MINIO_AUDIO_CHUNK_STORAGE_BUCKET_NAME=attendee-audio-chunks
   ```

## Fallback to AWS Variables

If you don't set the MinIO-specific variables, the system will fall back to AWS variables:
- `MINIO_ENDPOINT_URL` falls back to `AWS_ENDPOINT_URL`
- `MINIO_ACCESS_KEY` falls back to `AWS_ACCESS_KEY_ID`
- `MINIO_SECRET_KEY` falls back to `AWS_SECRET_ACCESS_KEY`
- `MINIO_RECORDING_STORAGE_BUCKET_NAME` falls back to `AWS_RECORDING_STORAGE_BUCKET_NAME`

This allows for easy migration from AWS S3 to MinIO.

## Example Docker Compose Configuration

Here's an example of how to configure MinIO with Docker Compose:

```yaml
version: '3.8'

services:
  minio:
    image: minio/minio
    container_name: minio
    environment:
      MINIO_ROOT_USER: your-access-key
      MINIO_ROOT_PASSWORD: your-secret-key
    ports:
      - "9000:9000"
      - "9001:9001"
    command: server /data --console-address ":9001"
    volumes:
      - minio_data:/data

  attendee:
    image: attendee
    environment:
      STORAGE_PROTOCOL: minio
      MINIO_ENDPOINT_URL: http://minio:9000
      MINIO_ACCESS_KEY: your-access-key
      MINIO_SECRET_KEY: your-secret-key
      MINIO_RECORDING_STORAGE_BUCKET_NAME: attendee-recordings
      # Other Attendee environment variables...
    depends_on:
      - minio

volumes:
  minio_data:
```

## Creating Buckets

Before starting Attendee, make sure to create the required buckets in MinIO:

1. Connect to your MinIO server (usually at `http://<minio-server>:9001` for the console)
2. Create the buckets specified in your environment variables
3. Ensure the access key and secret key have proper permissions

## SSL/TLS Configuration

If your MinIO server uses HTTPS, set the endpoint URL accordingly:
```bash
export MINIO_ENDPOINT_URL=https://minio-server:9000
```

Django's boto3 client (used by django-storages) will automatically handle SSL connections.

## Verification

To verify that MinIO is properly configured:

1. Start Attendee with the MinIO configuration
2. Create a bot that records a meeting
3. Check that the recording files are stored in your MinIO bucket
4. Verify you can access the files through the generated presigned URLs

## Troubleshooting

### Connection Issues

If you're having connection issues:
- Verify the MinIO server is running and accessible from the Attendee container
- Check that the endpoint URL is correct
- Ensure the access key and secret key are correct
- Verify the bucket exists and the credentials have proper permissions

### SSL Certificate Issues

If using HTTPS with self-signed certificates:
```bash
export MINIO_ENDPOINT_URL=https://minio-server:9000
# You may need to disable SSL verification (not recommended for production)
export AWS_S3_VERIFY_SSL=False  # This also affects MinIO
```

### Bucket Permission Issues

Ensure your MinIO credentials have the following permissions:
- `s3:ListBucket`
- `s3:PutObject`
- `s3:GetObject`
- `s3:DeleteObject`

## Migration from AWS S3

To migrate from AWS S3 to MinIO:

1. Set up your MinIO server
2. Create the required buckets
3. Update your environment variables to use MinIO
4. Restart Attendee

The system will automatically use MinIO for new uploads. Existing files in S3 will remain accessible if you keep the AWS credentials configured.

## Notes

- MinIO is fully S3-compatible, so all existing S3 features in Attendee work with MinIO
- Presigned URLs work the same way with MinIO as they do with AWS S3
- The storage configuration supports both path-style and virtual-host-style addressing
