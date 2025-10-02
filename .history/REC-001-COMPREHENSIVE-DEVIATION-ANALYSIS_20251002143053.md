# REC-001: Architecture Analysis for New Transcript Recorder Service

## Executive Summary

This document analyzes the current transcript-meeting-recorder implementation to inform the design of a new, purpose-built transcript recording service. Rather than attempting upstream alignment, we will build a modern, clean architecture that incorporates lessons learned from the current implementation while eliminating legacy complexity.

## **NEW SERVICE ARCHITECTURE REQUIREMENTS**

Based on analysis of current implementation, the new transcript recorder service should implement:

## **CURRENT IMPLEMENTATION ANALYSIS**

*The following sections analyze existing code to inform new service design*

---

## **COMPONENT 1: OpenStack Swift Storage Backend**
- **Primary Files**:
  - `bots/storage/infomaniak_storage.py` (116 lines)
  - `bots/storage/infomaniak_swift_utils.py` (114 lines)
  - `bots/storage/__init__.py` (22 lines)

### **Integration Points**
- **Settings Configuration**: `attendee/settings/base.py` lines 202-204
  ```python
  STORAGES = {
      "default": {
          "BACKEND": "bots.storage.InfomaniakSwiftStorage",
      }
  }
  SWIFT_CONTAINER_MEETS = os.getenv("SWIFT_CONTAINER_MEETS")
  ```

- **Model Usage**: `bots/models.py` lines 837, 1238
  ```python
  class RecordingStorage(InfomaniakSwiftStorage):
      pass
  
  class BotDebugScreenshotStorage(InfomaniakSwiftStorage):
      pass
  ```

- **Bot Controller**: `bots/bot_controller/bot_controller.py` line 280
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

### **New Service Requirements**
- **Clean Swift Interface**: Modern storage abstraction without Django Storage complexity
- **Async Operations**: Non-blocking upload/download with progress tracking
- **Configuration Management**: Environment-based config without hardcoded container names
- **Error Handling**: Robust retry logic and failure recovery
- **Monitoring**: Built-in metrics and health checks

---

## **COMPONENT 2: API Design with Metadata Support**

### **Code References**
- **Custom View**: `bots/bots_api_views.py` lines 144-200
  ```python
  class RecordingCreateView(APIView):
      def post(self, request):
          file_name = serializer.validated_data["file_name"]
          # Custom bot creation logic with file_name
  ```

- **URL Routing**: `bots/bots_api_urls.py` line 6
  ```python
  path("record", bots_api_views.RecordingCreateView.as_view(), name="record-create"),
  ```

- **Serializer Extension**: `bots/serializers.py` line 225
  ```python
  class CreateBotSerializer(serializers.Serializer):
      file_name = serializers.CharField(help_text="The name of the file to create")
  ```

### **Database Schema Impact**
- **Custom Field**: `Recording.file_name` field added to store filename
- **Usage**: `bots/bot_controller/bot_controller.py` lines 195-202
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

### **New Service Requirements**
1. **Native Metadata Support**: Built-in metadata field from day one
2. **Clean API Design**: RESTful endpoints without legacy baggage
3. **Flexible Recording Options**: Support for various recording formats and settings
4. **Validation**: Robust input validation and error responses
5. **Documentation**: Auto-generated API docs and examples

**Proposed API Design**:
```python
POST /api/v1/recordings
{
    "meeting_url": "https://zoom.us/j/123",
    "recording_name": "My Recording",
    "metadata": {
        "transcript_id": "transcript-abc123",
        "client_id": "client-xyz",
        "purpose": "weekly-standup"
    },
    "storage_settings": {
        "container": "transcript-meets",
        "filename": "transcript-abc123.mp4"
    },
    "webhook_url": "https://gateway.example.com/webhooks/recording-complete"
}
```

---

## **COMPONENT 3: Webhook-Based Integration System**

### **Code References**
- **Service Module**: `transcript_services/v1/api_service.py` (complete file)
  ```python
  def start_transcription(transcript_uuid):
      # Direct API call to /v1/record/done
  
  def could_not_record(transcript_id):
      # Direct API call to /v1/record/failed
  ```

- **Bot Controller Integration**: `bots/bot_controller/bot_controller.py` lines 14, 276, 291
  ```python
  import transcript_services.v1.api_service as transcript_api_service
  
  # On failure:
  transcript_api_service.could_not_record(transcript_id)
  
  # On success:
  transcript_api_service.start_transcription(transcript_id)
  ```

### **External Dependencies**
- **Environment Variables**:
  - `TRANSCRIPT_API_KEY` - API key for transcript service
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

### **New Service Requirements**
1. **Built-in Webhook System**: Native webhook delivery with retry logic
2. **Event-Driven Architecture**: Emit events for recording lifecycle
3. **Reliable Delivery**: Exponential backoff, dead letter queues
4. **Webhook Security**: Signature verification and authentication
5. **Monitoring**: Webhook delivery metrics and failure tracking

**Proposed Webhook Events**:
```python
# Recording started
{
    "event": "recording.started",
    "recording_id": "rec_123",
    "metadata": {"transcript_id": "transcript-abc123"},
    "timestamp": "2025-10-02T10:00:00Z"
}

# Recording completed successfully
{
    "event": "recording.completed",
    "recording_id": "rec_123", 
    "metadata": {"transcript_id": "transcript-abc123"},
    "file_url": "swift://container/transcript-abc123.mp4",
    "duration_seconds": 3600
}

# Recording failed
{
    "event": "recording.failed",
    "recording_id": "rec_123",
    "metadata": {"transcript_id": "transcript-abc123"},
    "error": "Meeting ended before recording started"
}
```

---

## **COMPONENT 4: Custom Bot Controller Logic**

### **Code References**
- **File Upload Logic**: `bots/bot_controller/bot_controller.py` lines 268-298
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

### **Custom Behavior**
1. **Filename Priority**: Uses `file_name` field if available, falls back to object ID
2. **Transcript ID Extraction**: Splits filename to extract transcript ID
3. **Direct Service Integration**: Calls transcript service directly from bot
4. **Swift-Specific Upload**: Uses custom Swift container environment variable

### **New Service Requirements**
1. **Simplified filename logic**: Clean, predictable naming patterns  
2. **Event-driven completion**: Emit completion events instead of direct service calls
3. **Configurable storage**: Support multiple storage backends
4. **Metadata-first design**: Extract context from metadata, not filenames

**Proposed File Management**:
```python
# Clean filename generation
def generate_filename(metadata):
    transcript_id = metadata.get('transcript_id')
    timestamp = metadata.get('created_at')
    return f"{transcript_id}_{timestamp}.mp4"

# Event emission on completion
def on_recording_complete(recording):
    emit_event('recording.completed', {
        'recording_id': recording.id,
        'metadata': recording.metadata,
        'file_url': recording.file_url
    })
```

---

## **COMPONENT 5: Custom Deployment Configuration**

### **Code References**
- **Helm Chart**: `charts/transcript-meeting-recorder/Chart.yaml`
  ```yaml
  name: transcript-meeting-recorder-api
  version: 0.0.3
  appVersion: "1.0.13_staging"
  ```

- **Custom Image**: `charts/transcript-meeting-recorder/values.yaml` lines 3-5
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

- **Config References**: Lines 39-40, templates/deployment.yaml
  ```yaml
  envFrom:
    configMapRef: transcript-config
    secretRef: transcript-secrets
  ```

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

### **New Service Requirements**
1. **Modern Container Patterns**: Multi-stage builds, minimal base images
2. **Cloud-native Configuration**: Environment-based config, no hardcoded values
3. **Observability**: Built-in metrics, health checks, structured logging
4. **Security**: Non-root containers, minimal privileges
5. **Scalability**: Horizontal scaling with stateless design

**Proposed Deployment Architecture**:
```yaml
# Modern Kubernetes deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: transcript-recorder
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: recorder
        image: transcript-recorder:latest
        env:
        - name: STORAGE_BACKEND
          value: "swift"
        - name: WEBHOOK_RETRY_ATTEMPTS
          value: "3"
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "1Gi" 
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
```

---

## **COMPONENT 6: Database Schema Extensions**

### **Code References**
- **Recording Model**: Custom `file_name` field
- **Storage Classes**: Custom storage backend references

### **Schema Differences**
| Field | Current | Upstream |
|-------|---------|----------|
| `Recording.file_name` | Custom CharField | Not present |
| Storage backend | Swift | S3-compatible |

### **New Service Requirements**
1. **Clean Schema Design**: Minimal, focused data model
2. **Metadata Storage**: JSON field for flexible metadata
3. **Multi-backend Support**: Storage backend abstraction
4. **Audit Trail**: Created/updated timestamps, user tracking

**Proposed Data Model**:
```python
class Recording(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    meeting_url = models.URLField()
    file_url = models.URLField(null=True, blank=True)
    
    # Core metadata field - replaces file_name pattern
    metadata = models.JSONField(default=dict)
    
    # Audit fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    
    # Storage backend (configurable)
    storage_backend = models.CharField(max_length=50, default='swift')
    
    class Meta:
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['created_by', '-created_at']),
        ]
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