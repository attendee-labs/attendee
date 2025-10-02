# REC-001: Code Deviation Analysis

## Executive Summary

This document provides a detailed analysis of every deviation from the upstream attendee project found in the transcript-meeting-recorder codebase. Each deviation is documented with specific code references, required migration actions, and complexity assessments.

---

## **DEVIATION 1: Custom OpenStack Swift Storage Backend**

### **Code References**
- **Primary Files**:
  - [`bots/storage/infomaniak_storage.py`](./bots/storage/infomaniak_storage.py) (116 lines)
  - [`bots/storage/infomaniak_swift_utils.py`](./bots/storage/infomaniak_swift_utils.py) (114 lines)
  - [`bots/storage/__init__.py`](./bots/storage/__init__.py) (22 lines)

### **Dependencies & Environment Variables**
| Dependency | Purpose | Environment Variable | Required |
|------------|---------|---------------------|----------|
| `python-swiftclient` | OpenStack Swift API client | - | Yes |
| OpenStack Swift | Object storage backend | `OS_AUTH_URL` | Yes |
| - | Authentication credentials | `OS_APPLICATION_CREDENTIAL_ID` | Yes |
| - | Authentication credentials | `OS_APPLICATION_CREDENTIAL_SECRET` | Yes |
| - | Region specification | `OS_REGION_NAME` | Yes |
| - | Container name | `SWIFT_CONTAINER_MEETS` | No (defaults to "transcript-meets") |

### **Integration Points**
- **Settings Configuration**: [`attendee/settings/base.py`](./attendee/settings/base.py) lines 202-204
  ```python
  STORAGES = {
      "default": {
          "BACKEND": "bots.storage.InfomaniakSwiftStorage",
      }
  }
  SWIFT_CONTAINER_MEETS = os.getenv("SWIFT_CONTAINER_MEETS")
  ```

- **Model Usage**: [`bots/models.py`](./bots/models.py) lines 837, 1238
  ```python
  class RecordingStorage(InfomaniakSwiftStorage):
      pass
  
  class BotDebugScreenshotStorage(InfomaniakSwiftStorage):
      pass
  ```

- **Bot Controller**: [`bots/bot_controller/bot_controller.py`](./bots/bot_controller/bot_controller.py) line 280
  ```python
  file_uploader = FileUploader(
      os.environ.get("SWIFT_CONTAINER_MEETS"),
      self.get_recording_filename(),
  )
  ```

### **Environment Variables Required**
- `OS_AUTH_URL` - OpenStack authentication URL
- `OS_APPLICATION_CREDENTIAL_ID` - Application credential ID
- `OS_APPLICATION_CREDENTIAL_SECRET` - Application credential secret
- `OS_REGION_NAME` - OpenStack region
- `SWIFT_CONTAINER_MEETS` - Swift container name (default: "transcript-meets")

### **Custom Features Implemented**
1. **Django Storage Interface**: Complete implementation of Django's Storage class
2. **Swift Authentication**: Application credential authentication flow
3. **Container Operations**: Upload, download, delete, list, exists functionality
4. **URL Generation**: Swift-specific URL generation for file access
5. **Error Handling**: Swift-specific exception handling

### **Migration Requirements**
- **Primary Strategy**: Contribute Swift support to upstream external storage system
- **Fallback**: Maintain minimal fork with only Swift storage difference (no S3 compatibility layer)
- **Approach**: "Rebase" strategy - fresh start with upstream, then re-implement custom features using upstream patterns

### **Data Flow Diagram: Swift Storage + Transcription Integration**

```mermaid
graph TD
    A[API Request with file_name] --> B[RecordingCreateView]
    B --> C[Create Bot + Recording with file_name]
    C --> D[Bot Controller starts recording]
    D --> E[Recording completes]
    E --> F[Extract transcript_id from file_name]
    F --> G[Upload to Swift Storage]
    G --> H{Upload Success?}
    H -->|Yes| I[Call transcript_api_service.start_transcription]
    H -->|No| J[Call transcript_api_service.could_not_record]
    I --> K[External Transcription Service]
    J --> L[Failure notification]
    
    style A fill:#e1f5fe
    style G fill:#fff3e0
    style K fill:#f3e5f5
    style L fill:#ffebee
```

**Flow Explanation:**
1. **API Entry**: Client sends recording request with custom `file_name` parameter
2. **Bot Creation**: Custom view creates bot and recording with stored filename
3. **Recording Process**: Bot controller handles meeting recording
4. **File Upload**: Completed recording uploaded to Swift storage using custom backend
5. **Transcription Trigger**: Direct API call to external transcription service
6. **Error Handling**: Failure notifications sent via direct API calls

---

## **DEVIATION 2: Custom API Endpoint with file_name Parameter**

### **Code References**
- **Custom View**: [`bots/bots_api_views.py`](./bots/bots_api_views.py) lines 144-200
  ```python
  class RecordingCreateView(APIView):
      def post(self, request):
          file_name = serializer.validated_data["file_name"]
          # Custom bot creation logic with file_name
  ```

- **URL Routing**: [`bots/bots_api_urls.py`](./bots/bots_api_urls.py) line 6
  ```python
  path("record", bots_api_views.RecordingCreateView.as_view(), name="record-create"),
  ```

- **Serializer Extension**: [`bots/serializers.py`](./bots/serializers.py) line 225
  ```python
  class CreateBotSerializer(serializers.Serializer):
      file_name = serializers.CharField(help_text="The name of the file to create")
  ```

### **Dependencies & Environment Variables**
| Dependency | Purpose | Environment Variable | Required |
|------------|---------|---------------------|----------|
| `Django REST Framework` | API view and serialization | - | Yes |
| `ApiKeyAuthentication` | Custom authentication | - | Yes |
| Database (Recording model) | Store file_name field | `DATABASE_URL` | Yes |

### **Database Schema Impact**
- **Custom Field**: `Recording.file_name` field added to store filename
- **Usage**: [`bots/bot_controller/bot_controller.py`](./bots/bot_controller/bot_controller.py) lines 195-202
  ```python
  def get_recording_filename(self):
      recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
      if recording.file_name:
          return f"{recording.file_name}.{self.bot_in_db.recording_format()}"
      else:
          return f"{recording.object_id}.{self.bot_in_db.recording_format()}"
  ```

### **API Usage Pattern**
Gateway service calls:
```python
POST /record
{
    "meeting_url": "https://zoom.us/j/123",
    "bot_name": "My Bot",
    "file_name": "transcript-abc123"  // Custom field
}
```

### **Migration Requirements**
1. **Remove custom endpoint**: Delete `RecordingCreateView`
2. **Remove custom serializer field**: Remove `file_name` from `CreateBotSerializer`
3. **Update Gateway integration**: Use upstream patterns:
   ```python
   {
       "meeting_url": "https://zoom.us/j/123",
       "bot_name": "My Bot",
       "metadata": {"transcript_id": "transcript-abc123"},
       "external_media_storage_settings": {
           "recording_file_name": "transcript-abc123.mp4"
       }
   }
   ```

---

## **DEVIATION 3: Direct Transcription Service Integration**

### **Code References**
- **Service Module**: [`transcript_services/v1/api_service.py`](./transcript_services/v1/api_service.py) (complete file)
  ```python
  def start_transcription(transcript_uuid):
      # Direct API call to /v1/record/done
  
  def could_not_record(transcript_id):
      # Direct API call to /v1/record/failed
  ```

- **Bot Controller Integration**: [`bots/bot_controller/bot_controller.py`](./bots/bot_controller/bot_controller.py) lines 14, 276, 291
  ```python
  import transcript_services.v1.api_service as transcript_api_service
  
  # On failure:
  transcript_api_service.could_not_record(transcript_id)
  
  # On success:
  transcript_api_service.start_transcription(transcript_id)
  ```

### **Dependencies & Environment Variables**
| Dependency | Purpose | Environment Variable | Required |
|------------|---------|---------------------|----------|
| `requests` | HTTP client for API calls | - | Yes |
| External Transcription API | Processing service | `TRANSCRIPT_API_KEY` | Yes |
| External Transcription API | Service endpoint | `TRANSCRIPT_API_URL` | Yes |
  - `TRANSCRIPT_API_URL` - Base URL for transcript service

- **Endpoint Dependencies**:
  - `POST /v1/record/done?transcript_id={id}` - Success notification
  - `POST /v1/record/failed?transcript_id={id}` - Failure notification

### **Integration Logic**
1. Extract transcript ID from filename: `transcript_id = self.get_recording_filename().split(".")[0]`
2. Check file size for validity
3. Upload file to Swift storage
4. Call appropriate transcript service endpoint
5. Delete local file

### **Migration Requirements**
1. **Remove direct API calls**: Delete `transcript_services` module
2. **Implement webhook endpoint**: Add to Gateway service
3. **Configure webhook subscription**: Use upstream webhook system
4. **Move integration logic**: From bot controller to Gateway webhook handler

---

## **DEVIATION 4: Custom Bot Controller Logic**

### **Code References**
- **File Upload Logic**: [`bots/bot_controller/bot_controller.py`](./bots/bot_controller/bot_controller.py) lines 268-298
  ```python
  def cleanup(self):
      # Custom filename generation
      logger.info("file_name: %s", self.get_recording_filename())
      transcript_id = self.get_recording_filename().split(".")[0]
      
      # Custom file uploader instantiation
      file_uploader = FileUploader(
          os.environ.get("SWIFT_CONTAINER_MEETS"),
          self.get_recording_filename(),
      )
      
      # Direct transcript service calls
      transcript_api_service.start_transcription(transcript_id)
  ```

- **Recording Filename Logic**: Lines 195-202
  ```python
  def get_recording_filename(self):
      recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
      if recording.file_name:
          return f"{recording.file_name}.{self.bot_in_db.recording_format()}"
      else:
          return f"{recording.object_id}.{self.bot_in_db.recording_format()}"
  ```

### **Dependencies & Environment Variables**
| Dependency | Purpose | Environment Variable | Required |
|------------|---------|---------------------|----------|
| Swift Storage Backend | File upload destination | `SWIFT_CONTAINER_MEETS` | Yes |
| Transcription Service | Direct API integration | `TRANSCRIPT_API_KEY`, `TRANSCRIPT_API_URL` | Yes |
| FileUploader class | Custom upload logic | - | Yes |

### **Custom Behavior**
1. **Filename Priority**: Uses `file_name` field if available, falls back to object ID
2. **Transcript ID Extraction**: Splits filename to extract transcript ID
3. **Direct Service Integration**: Calls transcript service directly from bot
4. **Swift-Specific Upload**: Uses custom Swift container environment variable

### **Migration Requirements**
1. **Simplify filename logic**: Use upstream recording naming
2. **Remove direct service calls**: Let upstream handle completion events
3. **Use external storage settings**: Replace hardcoded Swift container reference
4. **Leverage metadata**: Extract transcript ID from bot metadata instead of filename

---

## **DEVIATION 5: Custom Deployment Configuration**

### **Code References**
- **Helm Chart**: [`charts/transcript-meeting-recorder/Chart.yaml`](./charts/transcript-meeting-recorder/Chart.yaml)
  ```yaml
  name: transcript-meeting-recorder-api
  version: 0.0.3
  appVersion: "1.0.13_staging"
  ```

- **Custom Image**: [`charts/transcript-meeting-recorder/values.yaml`](./charts/transcript-meeting-recorder/values.yaml) lines 3-5
  ```yaml
  image:
    repository: vanyabrucker/transcript-meeting-recorder
    tag: "1.0.13_staging"
  ```

- **Environment Variables**: Lines 29-37
  ```yaml
  env:
    LAUNCH_BOT_METHOD: "kubernetes"
    K8S_CONFIG: transcript-config
    K8S_SECRETS: transcript-secrets
    K8S_DOCKER_SECRETS: docker-secrets
    BOT_POD_IMAGE: vanyabrucker/transcript-meeting-recorder
    CUBER_RELEASE_VERSION: "1.0.13_staging"
    CUBER_NAMESPACE: "apps"
  ```

- **Config References**: Lines 39-40, [`templates/deployment.yaml`](./charts/transcript-meeting-recorder/templates/deployment.yaml)
  ```yaml
  envFrom:
    configMapRef: transcript-config
    secretRef: transcript-secrets
  ```

### **Dependencies & Environment Variables**
| Dependency | Purpose | Environment/Config Variable | Required |
|------------|---------|----------------------------|----------|
| Helm | Kubernetes deployment tool | - | Yes |
| Custom Docker Image | Application container | `BOT_POD_IMAGE` | Yes |
| Kubernetes ConfigMap | Application config | `transcript-config` | Yes |
| Kubernetes Secret | Sensitive credentials | `transcript-secrets` | Yes |
| Docker Registry Access | Image pulling | `docker-secrets` | Yes |

### **Infrastructure Dependencies**
- **ConfigMap**: `transcript-config` - Application configuration
- **Secret**: `transcript-secrets` - Sensitive credentials including:
  - Database credentials (`DB_RECORDER_USER`, `DB_RECORDER_PASS`, `DB_RECORDER_NAME`)
  - Swift storage credentials
  - API keys

### **Deployment Patterns**
- **HPA Configuration**: Auto-scaling between 1-2 replicas based on CPU
- **Resource Limits**: Memory 1Gi, CPU 500m
- **Init Container**: Database migration runner
- **Service Configuration**: ClusterIP on port 80 → 8000

### **Migration Requirements**
1. **Update base image**: Change to upstream attendee image
2. **Align environment variables**: Match upstream configuration patterns
3. **Update config/secret names**: Use upstream naming conventions
4. **Test resource allocations**: Validate upstream resource requirements
5. **Verify HPA compatibility**: Ensure scaling works with upstream metrics

---

## **DEVIATION 6: Database Schema Extensions**

### **Code References**
- **Recording Model**: [`bots/models.py`](./bots/models.py) line 852
  ```python
  class Recording(models.Model):
      file_name = models.CharField(max_length=255, null=False, blank=False)
  ```
- **Storage Classes**: Custom storage backend references
- **Migrations**: [`bots/migrations/`](./bots/migrations/) - Schema change migrations

### **Dependencies & Environment Variables**
| Dependency | Purpose | Environment Variable | Required |
|------------|---------|---------------------|----------|
| Django ORM | Database abstraction | `DATABASE_URL` | Yes |
| PostgreSQL/MySQL | Database backend | `DB_HOST`, `DB_USER`, `DB_PASS` | Yes |
| Django Migrations | Schema versioning | - | Yes |

### **Schema Differences**
| Field | Current | Upstream |
|-------|---------|----------|
| `Recording.file_name` | Custom CharField | Not present |
| Storage backend | Swift | S3-compatible |

### **Migration Requirements**
1. **Data Migration**: Extract `file_name` to metadata
2. **Schema Cleanup**: Remove custom fields
3. **Reference Updates**: Update all file_name usage

---

## **QUICK SETUP REFERENCE**

### **Environment Variables Summary**
```bash
# Swift Storage
OS_AUTH_URL=https://auth.cloud.ovh.net/v3
OS_APPLICATION_CREDENTIAL_ID=your_credential_id
OS_APPLICATION_CREDENTIAL_SECRET=your_credential_secret
OS_REGION_NAME=your_region
SWIFT_CONTAINER_MEETS=transcript-meets

# Transcription Service  
TRANSCRIPT_API_KEY=your_api_key
TRANSCRIPT_API_URL=https://api.transcription-service.com

# Database
DATABASE_URL=postgresql://user:pass@host:port/dbname

# Kubernetes Deployment
LAUNCH_BOT_METHOD=kubernetes
K8S_CONFIG=transcript-config
K8S_SECRETS=transcript-secrets
BOT_POD_IMAGE=vanyabrucker/transcript-meeting-recorder
```

### **Required Packages**
```bash
pip install python-swiftclient
pip install requests
pip install django
pip install djangorestframework
```

---

## **MIGRATION COMPLEXITY ASSESSMENT**

| Component | Files Affected | Lines of Code | Complexity | Effort (Days) |
|-----------|----------------|---------------|------------|---------------|
| Swift Storage | 4 files | ~250 lines | High (4/5) | 8-10 |
| API Endpoints | 3 files | ~80 lines | Low (1/5) | 1-2 |
| Transcription Integration | 2 files | ~100 lines | Medium (2/5) | 3-4 |
| Bot Controller | 1 file | ~50 lines | Medium (3/5) | 2-3 |
| Deployment Config | 4 files | ~100 lines | High (4/5) | 5-6 |
| Database Schema | Multiple | N/A | Low (2/5) | 1-2 |

**Total Estimated Effort**: 20-27 days

---

## **RISK ANALYSIS**

### **High Risk**
1. **Swift Storage Compatibility**: Upstream may not support Swift natively
2. **Webhook Reliability**: Ensuring reliable delivery vs direct API calls
3. **Performance Impact**: Upstream may have different performance characteristics

### **Medium Risk**
1. **Configuration Complexity**: Environment variable mapping
2. **Integration Testing**: End-to-end workflow validation
3. **Deployment Coordination**: Multiple service updates required

### **Low Risk**
1. **API Pattern Changes**: Well-defined upstream alternatives
2. **Database Migration**: Straightforward field mapping
3. **Code Cleanup**: Removal of custom code

---

## **RECOMMENDED MIGRATION SEQUENCE**

**Migration Strategy**: "Rebase" approach with fresh upstream start

1. **Fresh Start**: Create new branch from upstream attendee repository, removing all custom code
2. **Swift Storage Contribution**: Evaluate and contribute OpenStack Swift implementation to upstream project
3. **Feature Re-implementation**: Methodically re-implement custom features using upstream patterns:
   - Use metadata system for filename passing
   - Leverage external storage framework for Swift integration
   - Implement webhook-based transcription triggers
   - Align deployment configurations with upstream standards
4. **Sync Process Establishment**: Define monthly upstream sync process with automated Linear task scheduling
5. **Testing and Validation**: Local Kubernetes testing followed by staging deployment
6. **Production Rollout**: Canary deployment and full production migration

---

## **CONCLUSION**

The analysis reveals 6 major deviation categories with varying complexity levels. The most significant challenges are the Swift storage integration and deployment configuration alignment. However, all deviations have clear migration paths using upstream features, particularly the metadata system and external storage framework.

**Key Success Factors**:
- Fresh upstream start with "rebase" strategy instead of complex merge resolution
- Early upstream contribution for Swift storage (no S3 compatibility layer needed)
- Methodical re-implementation using upstream patterns and metadata system
- Monthly sync process establishment with Linear automation
- Comprehensive webhook testing for reliable transcription integration

**Expected Outcome**:
- 60-70% reduction in custom code through upstream pattern adoption
- Full upstream compatibility with monthly sync capability
- Community contribution of Swift storage implementation
- Elimination of merge conflict complexity through fresh start approach