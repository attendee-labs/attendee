# Dockerfile updates for Azure Storage support
# These changes ensure the Docker image can properly use Azure Storage alongside AWS S3

# 1. Add system dependencies for Azure authentication (if using managed identity)
# 2. Ensure certificates are up-to-date for HTTPS connections to Azure
# 3. Add optional Azure CLI for debugging (development only)

# Here are the recommended additions to your Dockerfile:

# ================================
# Option 1: Minimal Production Changes (RECOMMENDED)
# ================================
# Add these lines after the existing apt-get install section (around line 35):

RUN apt-get update && apt-get install -y \
    ca-certificates-java \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# This ensures:
# - Up-to-date SSL certificates for Azure API calls
# - Clean apt cache to reduce image size

# ================================
# Option 2: Development/Debug Version (OPTIONAL)
# ================================
# If you need Azure CLI for debugging/development, add:

# Install Azure CLI (optional - for development/debugging only)
# RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# ================================
# Option 3: Multi-stage build optimization
# ================================
# Consider moving Azure requirements to a separate requirements file:

# In the deps stage, before "RUN pip install -r requirements.txt":
# COPY requirements_azure.txt .
# RUN pip install -r requirements_azure.txt

# ================================
# Environment Variables (Runtime Configuration)
# ================================
# These don't go in Dockerfile but in your deployment config:

# For AWS S3 only (current behavior):
# STORAGE_UPLOAD_MODE=s3

# For Azure Storage only:
# STORAGE_UPLOAD_MODE=azure
# AZURE_STORAGE_ACCOUNT_NAME=mystorageaccount
# AZURE_STORAGE_CONTAINER_NAME=recordings

# For both AWS S3 and Azure Storage:
# STORAGE_UPLOAD_MODE=both
# AWS_RECORDING_STORAGE_BUCKET_NAME=my-bucket
# AZURE_STORAGE_ACCOUNT_NAME=mystorageaccount  
# AZURE_STORAGE_CONTAINER_NAME=recordings

# ================================
# Health Check (Optional)
# ================================
# Add a health check to verify storage connectivity:
# HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
#   CMD python -c "from bots.bot_controller.unified_storage_manager import UnifiedStorageManager; print('Storage OK')"