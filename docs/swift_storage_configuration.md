## Swift Storage Configuration

Add these environment variables to your .env file to use Swift storage:

```bash
# Set the recording storage backend to Swift
RECORDING_STORAGE_BACKEND=SWIFT

# Swift authentication credentials
SWIFT_AUTH_URL=https://identity.your-swift-provider.com/v3
SWIFT_USERNAME=your-username
SWIFT_PASSWORD=your-password
SWIFT_TENANT_NAME=your-tenant-name
SWIFT_AUTH_VERSION=3

# Swift container configuration
SWIFT_CONTAINER_NAME=recordings
SWIFT_TEMP_URL_KEY=your-temp-url-key
SWIFT_BASE_URL=https://swift.your-provider.com

# Example for Infomaniak Swift Storage (Swiss provider)
# SWIFT_AUTH_URL=https://api.pub1.infomaniak.cloud/identity/v3
# SWIFT_USERNAME=PCU-your-project-id
# SWIFT_PASSWORD=your-password
# SWIFT_TENANT_NAME=sb_project_your-project-id
# SWIFT_AUTH_VERSION=3
# SWIFT_CONTAINER_NAME=recordings
# SWIFT_BASE_URL=https://s3.pub1.infomaniak.cloud
```