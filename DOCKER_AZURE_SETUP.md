# Docker Configuration for Azure Storage Support

## Overview
This guide explains the Docker configuration needed to support Azure Storage alongside AWS S3 in your Attendee Bot application.

## Dockerfile Changes Made

### ✅ Required Changes (Already Applied)
```dockerfile
# Update certificates for Azure HTTPS connections
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates
```

**Why this is needed:**
- Azure Storage APIs use HTTPS exclusively
- Ensures up-to-date SSL certificates for secure connections
- Required for proper Azure SDK functionality

## Docker Build & Run

### Build the Image
```bash
# Standard build (includes Azure support)
docker build -t attendee-bot:latest .

# Or build with specific Azure requirements
docker build -t attendee-bot:azure --target=build .
```

### Environment Variables for Docker

#### 1. AWS S3 Only (Current/Default)
```bash
docker run -e STORAGE_UPLOAD_MODE=s3 \
           -e AWS_RECORDING_STORAGE_BUCKET_NAME=my-recordings \
           -e AWS_ACCESS_KEY_ID=AKIA... \
           -e AWS_SECRET_ACCESS_KEY=xxx... \
           attendee-bot:latest
```

#### 2. Azure Storage Only
```bash
docker run -e STORAGE_UPLOAD_MODE=azure \
           -e AZURE_STORAGE_ACCOUNT_NAME=myrecordingstorage \
           -e AZURE_STORAGE_CONTAINER_NAME=recordings \
           -e AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=..." \
           attendee-bot:latest
```

#### 3. Both AWS S3 and Azure Storage
```bash
docker run -e STORAGE_UPLOAD_MODE=both \
           -e AWS_RECORDING_STORAGE_BUCKET_NAME=my-recordings \
           -e AWS_ACCESS_KEY_ID=AKIA... \
           -e AWS_SECRET_ACCESS_KEY=xxx... \
           -e AZURE_STORAGE_ACCOUNT_NAME=myrecordingstorage \
           -e AZURE_STORAGE_CONTAINER_NAME=recordings \
           -e AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=..." \
           attendee-bot:latest
```

## Docker Compose Configuration

### Example docker-compose.yml
```yaml
version: '3.8'

services:
  attendee-bot:
    build: .
    environment:
      # Storage Configuration
      STORAGE_UPLOAD_MODE: both  # s3, azure, or both
      
      # AWS S3 Configuration
      AWS_RECORDING_STORAGE_BUCKET_NAME: my-recordings-bucket
      AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID}
      AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY}
      AWS_DEFAULT_REGION: us-east-1
      
      # Azure Storage Configuration
      AZURE_STORAGE_ACCOUNT_NAME: myrecordingstorage
      AZURE_STORAGE_CONTAINER_NAME: recordings
      AZURE_STORAGE_CONNECTION_STRING: ${AZURE_STORAGE_CONNECTION_STRING}
      
      # Other existing environment variables
      DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY}
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: ${REDIS_URL}
    
    volumes:
      - ./logs:/attendee/logs
    
    depends_on:
      - redis
      - postgres

  redis:
    image: redis:7-alpine
    
  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: attendee
      POSTGRES_USER: attendee
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
```

### Environment File (.env)
```bash
# Storage Mode
STORAGE_UPLOAD_MODE=both

# AWS Credentials
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=xxx...

# Azure Storage
AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=myrecordingstorage;AccountKey=xxx==;EndpointSuffix=core.windows.net"

# Other app config
DJANGO_SECRET_KEY=your-secret-key
DATABASE_URL=postgres://attendee:password@postgres:5432/attendee
REDIS_URL=redis://redis:6379
POSTGRES_PASSWORD=secure-password
```

## Azure-Specific Docker Considerations

### 1. Managed Identity Support
When running in Azure Container Instances (ACI) or Azure Container Apps (ACA):

```yaml
# docker-compose.azure.yml
services:
  attendee-bot:
    build: .
    environment:
      STORAGE_UPLOAD_MODE: both
      
      # Azure Storage with Managed Identity
      AZURE_STORAGE_ACCOUNT_NAME: myrecordingstorage
      AZURE_STORAGE_CONTAINER_NAME: recordings
      # No connection string needed - uses managed identity
      
      # AWS Configuration
      AWS_RECORDING_STORAGE_BUCKET_NAME: my-recordings-bucket
```

### 2. Multi-Architecture Builds
For ARM64 support (Azure Container Apps):
```bash
docker buildx build --platform linux/amd64,linux/arm64 -t attendee-bot:multi .
```

### 3. Health Checks
Add health check for storage connectivity:
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "
import os
from bots.bot_controller.unified_storage_manager import UnifiedStorageManager
manager = UnifiedStorageManager('health-check')
assert manager.has_storage_configured, 'No storage configured'
print('Storage connectivity: OK')
"
```

## Deployment Scenarios

### 1. Local Development
```bash
# Use docker-compose for local testing
docker-compose up --build

# Test with Azure Storage only
STORAGE_UPLOAD_MODE=azure docker-compose up
```

### 2. Azure Container Instances
```bash
# Create resource group
az group create --name rg-attendee --location eastus

# Create container with managed identity
az container create \
  --resource-group rg-attendee \
  --name attendee-bot \
  --image attendee-bot:latest \
  --assign-identity \
  --environment-variables \
    STORAGE_UPLOAD_MODE=azure \
    AZURE_STORAGE_ACCOUNT_NAME=myrecordingstorage \
    AZURE_STORAGE_CONTAINER_NAME=recordings
```

### 3. Azure Container Apps
```bash
# Create container app environment
az containerapp env create \
  --name attendee-env \
  --resource-group rg-attendee \
  --location eastus

# Deploy with managed identity
az containerapp create \
  --name attendee-bot \
  --resource-group rg-attendee \
  --environment attendee-env \
  --image attendee-bot:latest \
  --assign-identity \
  --env-vars \
    STORAGE_UPLOAD_MODE=both \
    AZURE_STORAGE_ACCOUNT_NAME=myrecordingstorage \
    AZURE_STORAGE_CONTAINER_NAME=recordings
```

## Troubleshooting

### Common Docker Issues

#### 1. Azure SDK Import Errors
```bash
# Check if Azure packages are installed
docker run attendee-bot:latest python -c "import azure.storage.blob; print('Azure SDK OK')"
```

#### 2. Certificate Issues
```bash
# Verify certificates are updated
docker run attendee-bot:latest python -c "
import ssl
import certifi
print('Cert bundle:', certifi.where())
print('SSL context:', ssl.create_default_context())
"
```

#### 3. Connection Testing
```bash
# Test storage connectivity
docker run -e AZURE_STORAGE_ACCOUNT_NAME=test -e AZURE_STORAGE_CONTAINER_NAME=test \
  attendee-bot:latest python -c "
from bots.bot_controller.unified_storage_manager import UnifiedStorageManager
manager = UnifiedStorageManager('test')
print('Providers:', manager.configured_providers)
"
```

### Log Analysis
```bash
# View storage-related logs
docker logs attendee-bot 2>&1 | grep -i "storage\|azure\|upload"

# Real-time storage logs
docker logs -f attendee-bot | grep -i "unified storage\|azure\|upload"
```

## Security Best Practices

### 1. Secrets Management
```bash
# Use Docker secrets for sensitive data
echo "your-connection-string" | docker secret create azure_storage_conn -
```

### 2. Network Security
```yaml
# Restrict network access
services:
  attendee-bot:
    networks:
      - internal
    environment:
      AZURE_STORAGE_ACCOUNT_NAME: myrecordingstorage

networks:
  internal:
    driver: bridge
```

### 3. Resource Limits
```yaml
services:
  attendee-bot:
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: '1.0'
        reservations:
          memory: 1G
          cpus: '0.5'
```

## Performance Optimization

### 1. Multi-stage Build
```dockerfile
FROM attendee-base AS azure-deps
COPY requirements_azure.txt .
RUN pip install -r requirements_azure.txt

FROM azure-deps AS production
COPY . .
```

### 2. Layer Caching
```dockerfile
# Copy requirements first for better caching
COPY requirements.txt requirements_azure.txt ./
RUN pip install -r requirements.txt -r requirements_azure.txt
```

This Docker configuration ensures your container can successfully upload to both AWS S3 and Azure Storage with minimal overhead and maximum flexibility.