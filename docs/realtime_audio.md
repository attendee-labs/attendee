# Realtime Audio Input and Output

Attendee supports bidirectional realtime audio streaming through websockets. You can receive mixed or per-participant audio from meetings and have your bot output audio into meetings in real-time.

## Setup

To enable realtime audio streaming, configure the `websocket_settings.audio` parameter when creating a bot:

```json
{
  "meeting_url": "https://meet.google.com/abc-def-ghi",
  "bot_name": "Audio Bot",
  "websocket_settings": {
    "audio": {
      "url": "wss://your-server.com/attendee-websocket",
      "sample_rate": 16000
    }
  }
}
```

The `sample_rate` can be `8000`, `16000`, or `24000` and defaults to `16000`. It determines the sample rate of the audio chunks you receive from Attendee.

## Websocket Message Format

### Outgoing Audio (Attendee → Your Websocket Server)

Your WebSocket server will receive messages in this format.

```json
{
  "bot_id": "bot_12345abcdef",
  "trigger": "realtime_audio.mixed",
  "data": {
    "chunk": "UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAABACAAAGRLVEE...",
    "sample_rate": 16000,
    "timestamp_ms": 1703123456789
  }
}
```

The `chunk` field is base64-encoded 16-bit single channel PCM audio data at the frequency specified in the `sample_rate` field.

### Incoming Audio (Your Websocket Server → Attendee)

When you want the bot to speak audio in the meeting, send a message in this format.

```json
{
  "trigger": "realtime_audio.bot_output",
  "data": {
    "chunk": "UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAABACAAAGRLVEE...",
    "sample_rate": 16000
  }
}
```

The `chunk` field is base64-encoded 16-bit single-channel PCM audio data. The sample rate can be `8000`, `16000` or `24000`.

## Integration with Voice Agent APIs

The realtime audio streaming can be easily integrated with voice agent APIs to bring voice agents into meetings.

### Deepgram Voice Agent API
Connect directly to Deepgram's voice agent WebSocket API by forwarding audio chunks. Set an output sample rate of `16000` to be compatible with Deepgram's real-time streaming requirements. See an example app showing how to integrate with Deepgram's voice agent API [here](https://github.com/attendee-labs/voice-agent-example).

### OpenAI Realtime API
Connect directly to OpenAI's realtime API by forwarding audio chunks. Set an output sample rate of `24000` to be compatible with OpenAI's real-time streaming requirements.

## Code Samples

A simple example app showing how to integrate with Deepgram's voice agent API: https://github.com/attendee-labs/voice-agent-example

## Per Participant Audio Streaming

Instead of receiving a single mixed audio stream, you can receive separate audio streams for each individual participant by configuring the `websocket_settings.per_participant_audio` parameter when creating a bot:

```json
{
  "meeting_url": "https://meet.google.com/abc-def-ghi",
  "bot_name": "Audio Bot",
  "websocket_settings": {
    "per_participant_audio": {
      "url": "wss://your-server.com/attendee-websocket",
      "sample_rate": 16000
    }
  }
}
```

The `sample_rate` can be `8000` or `16000` and defaults to `16000`.

The websocket message payload is identical to the mixed audio payload, except the `trigger` is `realtime_audio.per_participant` and the `data` object includes a `participant_uuid` field identifying which participant the audio belongs to. To resolve a `participant_uuid` to a full participant object subscribe to the `participant_events.join_leave` webhook event which will send the full participant object when they join the meeting.

```json
{
  "bot_id": "bot_12345abcdef",
  "trigger": "realtime_audio.per_participant",
  "data": {
    "participant_uuid": "participant_abc123",
    "chunk": "UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAABACAAAGRLVEE...",
    "sample_rate": 16000,
    "timestamp_ms": 1703123456789
  }
}
```

 See [here](https://github.com/attendee-labs/realtime-per-participant-video-and-audio-example) for an example program showing how use per-participant audio streaming.

## Retries on Websocket Connections

Attendee will automatically retry to connect to your websocket server if the connection is lost or the initial connection attempt fails. We will retry up to 30 times with a 2 second delay between retries.

## Error Messages

Currently, we don't give any feedback on errors with the websocket connection or invalid message formats. We plan to improve this in the future.


