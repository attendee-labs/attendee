# Desktop Recorder SDK API

REST API that lets a **desktop recorder SDK** (Windows / macOS / Linux) record a meeting
locally on the user's machine and hand the media to the Attendee backend for storage and
transcription.

## Mental model: it's `BotController`'s upload half, relocated to the client

The existing server-side recorder runs inside a trusted bot pod
([`BotController`](../bots/bot_controller/bot_controller.py)):

1. A platform adapter joins the meeting.
2. GStreamer captures screen + audio to a **local file on the pod**
   ([`screen_and_audio_recorder.py`](../bots/bot_controller/screen_and_audio_recorder.py)).
3. On `cleanup()` the local file is pushed to S3
   ([`s3_file_uploader.py`](../bots/bot_controller/s3_file_uploader.py) — batch;
   [`streaming_uploader.py`](../bots/bot_controller/streaming_uploader.py) — hand-rolled S3
   multipart), then `recording_file_saved(s3_key)` sets `recording.file`, which async
   transcription reads.

The desktop recorder is **the capture + upload half of that pipeline, moved from a trusted
pod to an untrusted, unreliable client.** Everything downstream of `recording.file`
(transcription, transcript/recording read endpoints, webhooks) is reused unchanged.

Only two things change because the client is untrusted and unreliable:

| Concern | Server pod (today) | Desktop client (this API) |
|---|---|---|
| S3 credentials | Direct AWS creds (boto3) | Client can't hold AWS creds → **presigned multipart URLs** |
| Reliability | Pod stable, `cleanup()` runs once | Crashes / network drops → **resumable multipart + reaper** |
| The seam | `recording_file_saved()` in-process | Client calls `POST /complete` → backend runs the same handoff |

## Scope (v1)

- **Batch upload**: SDK uploads the finished (or partial) recording after the meeting.
  Realtime streaming / live captions are out of scope.
- **Auth**: existing project **API key** (`Authorization: Token <api_key>`). A recorder
  session belongs to the key's project, exactly like every other `/api/v1/*` endpoint.
- Audio and/or video (driven by `content_type`); reuses `RecordingStorage` for storage and
  the existing recording **download** endpoint.
- **Transcription is deferred to v2.** The existing async transcription pipeline builds
  utterances from **per-participant `audio_chunks`**
  ([`process_async_transcription_task.py`](../bots/tasks/process_async_transcription_task.py)),
  which a desktop recorder's single mixed file does not produce. Transcribing an uploaded
  mixed file needs a new file-based transcription path (Deepgram/AssemblyAI whole-file +
  diarization) — out of scope for v1. v1 stores and serves the media only.

## Flow

```
SDK records locally
  └─ POST /api/v1/recorder_sessions              -> { object_id, upload_id, part_urls[] }
  └─ PUT each part to S3 (resumable, idempotent by part number)
  └─ POST /api/v1/recorder_sessions/{id}/complete { parts:[{part_number, etag}] }
        backend: CompleteMultipartUpload -> recording.file = key -> enqueue transcription
  └─ webhook fires on done/failed  (reliable, works even if SDK is offline)
  └─ GET /api/v1/recorder_sessions/{id}          (poll: upload + transcription state)
  └─ GET /api/v1/recorder_sessions/{id}/transcript
  └─ GET /api/v1/recorder_sessions/{id}/recording
```

## Data model

- New `SessionTypes.DESKTOP_RECORDING`. A recorder session is a `Bot` row with this
  `session_type` (no meeting URL), plus a `Recording` (default, `NON_REALTIME`).
- New `RecorderUpload` model holding the upload lifecycle:
  `session (Bot FK)`, `object_id`, `s3_key`, `upload_id` (S3 multipart id),
  `state`, `content_type`, `bytes_expected`, `bytes_received`, `parts` (JSON),
  `last_activity_at`, `failure_data`.
- Upload states: `created` → `uploading` → `uploaded` → `processing` → `complete`,
  with terminal `failed` / `expired`. Recording/transcription reuse existing
  `RecordingStates` / `RecordingTranscriptionStates`.

## Failure & interruption model (first-class requirement)

| Failure | Handling |
|---|---|
| Recording interrupted mid-meeting | Batch tolerates it: upload whatever bytes exist; empty/too-small upload rejected at `complete`. |
| Upload interrupted (network / sleep) | S3 **multipart**; each part idempotent by number, just re-PUT the failed part. |
| App restart mid-upload | Resumable: SDK persists `{object_id, upload_id, parts}`; `GET /{id}` returns already-received parts (S3 `ListParts`). |
| Duplicate `create` / retries | `deduplication_key` unique constraint → returns the existing session, never a duplicate. |
| `complete` called twice | Idempotent: already-finalized returns current state, no re-enqueue. |
| Uploaded to S3 but `complete` never called (client vanishes) | **Reaper** command aborts the multipart upload (stops S3 charging for orphaned parts) and marks `expired`. |
| Session created, nothing uploaded | Same reaper, shorter TTL. |
| Corrupt / truncated media | `complete` checks size > 0; processing probes the file before transcription; failure → `failed` + reason, media retained. |
| Transcription fails | Existing `RecordingTranscriptionStates.FAILED` + `transcription_failure_data`; task retries; `POST /{id}/retry` to re-drive. |
| Out of credits | Checked at create and before processing; if depleted after upload, held (not discarded). |
| SDK offline at completion | v1: completion observed via `GET /{id}` polling. Auto-firing a completion webhook is v2 (avoids coupling the new session type to the `BotEventManager` state machine). |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/recorder_sessions` | Create session; returns `object_id`, `upload_id`, presigned PUT part URLs. Accepts `metadata`, `transcription_settings`, `deduplication_key`, `content_type`, `bytes_expected`. |
| `GET` | `/api/v1/recorder_sessions/{id}` | Status: upload + recording + transcription state, received parts (resume). |
| `POST` | `/api/v1/recorder_sessions/{id}/parts` | Fetch more presigned part URLs for long recordings. |
| `POST` | `/api/v1/recorder_sessions/{id}/complete` | Finalize multipart, attach file, mark recording complete, fire webhook. Idempotent. |
| `POST` | `/api/v1/recorder_sessions/{id}/abort` | SDK-initiated cancel → abort multipart, mark `expired`. |
| `GET` | `/api/v1/recorder_sessions/{id}/recording` | Short-lived download URL for the stored media. |
| `GET` | `/api/v1/account` | SDK init / key validation: project + org + credits. |

`GET /transcript` and a transcription-retry endpoint are intentionally omitted in v1
(transcription deferred to v2).

## Background reaping

`python manage.py clean_up_abandoned_recorder_sessions` (mirrors
[`clean_up_bots_with_heartbeat_timeout_or_that_never_launched`](../bots/management/commands/clean_up_bots_with_heartbeat_timeout_or_that_never_launched.py)),
run periodically by the scheduler: aborts orphaned multipart uploads, expires stale
sessions, re-enqueues stuck `uploaded` → processing transitions.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `RECORDER_UPLOAD_URL_EXPIRY_SECONDS` | `3600` | Presigned PUT URL TTL |
| `RECORDER_MULTIPART_PART_SIZE_BYTES` | `8388608` (8 MB) | Part size guidance returned to the SDK |
| `RECORDER_SESSION_ABANDON_TTL_MINUTES` | `120` | Reaper idle threshold |
| `RECORDER_MAX_UPLOAD_BYTES` | `5368709120` (5 GB) | Reject oversized uploads |

S3 bucket / credentials are already configured via `RecordingStorage`.
`external_media_storage_settings` already lets a session write to the customer's own bucket.

## Out of scope (v1)

Realtime streaming / live captions; user-level (OAuth) auth — v1 uses the project API key.
