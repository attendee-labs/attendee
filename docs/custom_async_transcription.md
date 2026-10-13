## Custom Async Transcription

The custom async transcription provider allows you to use your own self-hosted transcription service. This is ideal if you want full control over your transcription infrastructure, need to keep audio data on-premises, or want to use a custom transcription model.

Unlike other providers, this does not require credentials in the dashboard. Instead, you configure your service endpoint using environment variables.

#### How it works

1. Attendee sends audio segments as raw PCM audio via HTTP POST to your configured endpoint
2. Your service processes the audio and returns the transcription asynchronously
3. The response must follow the expected format (see below)

#### Configuration

Set these environment variables on your Attendee server:

- `CUSTOM_ASYNC_TRANSCRIPTION_URL` **(required)**: The full URL of your transcription endpoint (e.g., `https://192.168.0.1/transcribe`)
- `CUSTOM_ASYNC_TRANSCRIPTION_TIMEOUT` (optional): Request timeout in seconds (default: 120)

#### Expected API format

Your transcription service must accept a `POST` request with `multipart/form-data` containing:

- `audio`: The audio file (sent as raw PCM audio, 16-bit linear PCM)
- `sample_rate`: The sample rate of the audio file in Hz
- Any additional custom parameters you specify in `transcription_settings`

**Audio format details:**
- Format: Raw PCM (Pulse Code Modulation)
- Sample width: 16-bit
- Encoding: linear16
- Sample rate: Depends on the meeting source (typically 16000 Hz or 32000 Hz)
- Channels: 1 (mono)

**Example request from Attendee to your service:**

```bash
curl -X POST 'http://your-service.com/transcribe' \
  -F 'audio=@audio.pcm' \
  -F 'language=fr-FR' \
  -F 'custom_param=value'
```

**Expected response format:**

Your service must return a JSON response with this structure:

```json
{
  "status": "done",
  "result": {
    "transcription": {
      "full_transcript": "The complete transcription text",
      "utterances": [
        {
          "words": [
            {
              "word": "hello",
              "start": 0.0,
              "end": 0.5
            },
            {
              "word": "world",
              "start": 0.6,
              "end": 1.0
            }
          ]
        }
      ]
    }
  }
}
```

**Response fields:**

- `status`: Must be `"done"` for successful transcription, or `"error"` for failures
- `result.transcription.full_transcript`: The complete transcription text
- `result.transcription.utterances`: Array of utterance objects
- `result.transcription.utterances[].words`: Array of word objects with timestamps
- `result.transcription.utterances[].words[].word`: The word text
- `result.transcription.utterances[].words[].start`: Start time in seconds
- `result.transcription.utterances[].words[].end`: End time in seconds

**Error response format:**

```json
{
  "status": "error",
  "error_code": "TRANSCRIPTION_FAILED"
}
```

#### Usage example

When creating a bot, specify the `custom_async_v2` provider in `transcription_settings`:

```json
{
  "meeting_url": "https://zoom.us/j/123456789",
  "bot_name": "My Bot",
  "transcription_settings": {
    "custom_async_v2": {
      "headers": {
        "X-Custom-Header": "value"
      },
      "form_data": {
        "language": "fr-FR",
        "model": "whisper-large-v3",
        "custom_param": "any_value"
      }
    }
  }
}
```

Entries under `headers` are sent as HTTP request headers (e.g. for auth tokens), and entries under `form_data` are sent as multipart form fields alongside the audio file. You can add any custom parameters your service needs.

**Minimal example (no custom parameters):**

```json
{
  "meeting_url": "https://zoom.us/j/123456789",
  "bot_name": "My Bot",
  "transcription_settings": {
    "custom_async_v2": {}
  }
}
```

#### Notes

- No credentials are needed in the Attendee dashboard
- Your service must respond asynchronously within the timeout period
- Audio is sent as raw PCM format (16-bit linear PCM, mono)
- The sample rate varies based on the meeting source (typically 16000 Hz or 32000 Hz)
- Word-level timestamps are supported if your service provides them
- You have full control over the transcription model, language detection, and processing