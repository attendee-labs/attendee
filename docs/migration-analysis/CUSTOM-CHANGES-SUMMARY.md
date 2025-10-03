# Meeting Recorder Fork - Custom Changes Inventory

## Overview
This document provides a structured inventory of all custom changes in the transcript-meeting-recorder fork, organized by change type and impact level.

## Summary Statistics
- **Total Custom Files:** 15+ files with significant modifications
- **New Modules:** 4 complete modules
- **Modified Core Files:** 8+ upstream files with custom logic
- **External Dependencies:** 3 new service integrations

---

## Detailed Inventory

| File/Module | Change Type | Description | Dependencies | Lines of Code | Impact Level |
|-------------|-------------|-------------|--------------|---------------|--------------|
| **STORAGE BACKEND** | | | | | |
| `bots/storage/infomaniak_storage.py` | New Feature | Complete OpenStack Swift storage backend implementation | `python-swiftclient` | 116 | HIGH |
| `bots/storage/infomaniak_swift_utils.py` | New Feature | Swift utility functions and helpers | `python-swiftclient` | 114 | HIGH |
| `bots/storage/__init__.py` | New Feature | Storage module initialization and exports | None | 22 | MEDIUM |
| **API MODIFICATIONS** | | | | | |
| `bots/bots_api_views.py` | Modified View | Custom `RecordingCreateView` with `file_name` parameter | Django REST Framework | ~60 lines custom | HIGH |
| `bots/serializers.py` | Modified Serializer | Added `file_name` field and validation to `CreateBotSerializer` | Django REST Framework | ~20 lines custom | MEDIUM |
| `bots/models.py` | Modified Model | Added `file_name` CharField to `Recording` model | Django ORM | 1 field | MEDIUM |
| **TRANSCRIPTION INTEGRATION** | | | | | |
| `transcript_services/v1/api_service.py` | New Feature | Direct API client for external transcription service | `requests` | 81 | HIGH |
| `bots/bot_controller/bot_controller.py` | Modified Logic | Integration with transcript service after upload | `transcript_services` | ~30 lines custom | HIGH |
| **DEPLOYMENT CONFIGURATION** | | | | | |
| `charts/transcript-meeting-recorder/Chart.yaml` | New Feature | Custom Helm chart metadata | Helm/Kubernetes | 6 | MEDIUM |
| `charts/transcript-meeting-recorder/values.yaml` | New Feature | Custom deployment values and configuration | Helm/Kubernetes | 40 | MEDIUM |
| `charts/transcript-meeting-recorder/templates/` | New Feature | Complete Kubernetes deployment templates | Helm/Kubernetes | 200+ | HIGH |
| **CONFIGURATION CHANGES** | | | | | |
| `attendee/settings/base.py` | Modified Settings | Swift storage backend configuration | Django Settings | ~5 lines custom | MEDIUM |
| `requirements.txt` | Modified Dependencies | Added `python-swiftclient` dependency | pip/Python | 1 line | LOW |
| **DATABASE MIGRATIONS** | | | | | |
| `bots/migrations/` | New Migrations | Database schema changes for `file_name` field | Django Migrations | 50+ | MEDIUM |

---

## External Dependencies Added

| Dependency | Purpose | Integration Point | Required Environment Variables |
|------------|---------|-------------------|-------------------------------|
| `python-swiftclient` | OpenStack Swift storage access | `bots.storage.InfomaniakSwiftStorage` | `OS_AUTH_URL`, `OS_APPLICATION_CREDENTIAL_ID`, `OS_APPLICATION_CREDENTIAL_SECRET`, `OS_REGION_NAME` |
| External Transcription API | Automated transcription triggering | `transcript_services.v1.api_service` | `TRANSCRIPT_API_KEY`, `TRANSCRIPT_API_URL` |
| Custom Container Registry | Docker image hosting | Helm chart deployment | `docker-secrets` (Kubernetes secret) |

---

## Critical Integration Points

### 1. Storage Backend Integration
- **Location:** `attendee/settings/base.py`
- **Impact:** Replaces default Django storage with Swift
- **Configuration:**
  ```python
  STORAGES = {
      "default": {
          "BACKEND": "bots.storage.InfomaniakSwiftStorage",
      }
  }
  ```

### 2. Bot Controller Integration
- **Location:** `bots/bot_controller/bot_controller.py` lines 270-291
- **Impact:** Triggers transcription after successful upload
- **Logic Flow:**
  1. Extract transcript ID from filename
  2. Upload file to Swift storage
  3. Call external transcription API

### 3. API Parameter Extension
- **Location:** `bots/bots_api_views.py` RecordingCreateView
- **Impact:** Accepts custom `file_name` parameter
- **Usage:** Client applications pass transcript IDs via filename

### 4. Database Schema Extension
- **Location:** `bots/models.py` Recording model
- **Impact:** Stores custom filename for each recording
- **Field:** `file_name = models.CharField(max_length=255, null=False, blank=False)`

---

## Risk Assessment

### HIGH RISK (Major Custom Logic)
- OpenStack Swift storage implementation (230+ lines)
- Transcription service integration (100+ lines)
- Custom API endpoints with file_name parameter

### MEDIUM RISK (Configuration/Schema Changes)
- Database schema modifications
- Deployment configuration
- Settings and environment variables

### LOW RISK (Dependencies)
- Added Python packages
- Environment variable additions

---

## Migration Complexity

### Complex (Significant Refactoring Required)
1. **Swift Storage Backend** - Complete reimplementation needed
2. **Transcription Integration** - Replace with webhook-based approach
3. **API Modifications** - Align with upstream parameter structure

### Moderate (Configuration Alignment)
1. **Deployment Charts** - Update to upstream patterns
2. **Database Schema** - Migrate data and remove custom fields
3. **Settings Configuration** - Align with upstream storage framework

### Simple (Direct Replacement)
1. **Dependencies** - Update requirements.txt
2. **Environment Variables** - Map to upstream equivalents

---

## Recommended Next Steps

1. **Impact Analysis:** Assess downstream systems using `file_name` parameter
2. **Data Migration Planning:** Plan extraction of filenames to metadata
3. **Storage Integration:** Evaluate upstream storage framework compatibility
4. **API Alignment:** Design migration path for client applications
5. **Testing Strategy:** Plan comprehensive testing of migration approach