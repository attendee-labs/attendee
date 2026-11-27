# Realtime Video

Attendee supports realtime per-participant video streaming through websockets. You can receive individual video frames for each participant in a meeting, from both webcam and screenshare. 

## Setup

To enable realtime video streaming, configure the `websocket_settings.per_participant_video` parameter when creating a bot:

```json
{
  "meeting_url": "https://us06web.zoom.us/j/12345678",
  "bot_name": "Video Bot",
  "websocket_settings": {
    "per_participant_video": {
      "url": "wss://your-server.com/attendee-websocket",
      "webcam_resolution": "360p",
      "screenshare_resolution": "720p"
    }
  }
}
```

You can configure the resolution independently for each video source using `webcam_resolution` and `screenshare_resolution`. Supported values are `"360p"`, `"720p"`, `"1080p"`, and `"none"` (to disable that source). Both default to `"360p"`.

## Websocket Message Format

Your WebSocket server will receive messages in this format.

```json
{
  "bot_id": "bot_12345abcdef",
  "trigger": "realtime_video.per_participant",
  "data": {
    "participant_uuid": "participant_abc123",
    "frame": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQ...",
    "format": "jpeg",
    "source": "webcam"
  }
}
```

The `frame` field is a base64-encoded JPEG image at the resolution you configured for that source. The `source` field is either `"webcam"` or `"screenshare"`. Frames are delivered at 2 FPS for 360p, or 1 FPS for 720p and 1080p.

To resolve a `participant_uuid` to a full participant object, subscribe to the `participant_events.join_leave` webhook event which will send the full participant object when they join the meeting.

## Participant Selection

Which participants' video you receive depends on the meeting platform.

### Zoom

Attendee delivers video from up to 8 participants at a time, prioritized by how recently each participant was the active speaker. Screenshare video is always delivered.

### Google Meet and Microsoft Teams

On Google Meet and Teams, Attendee captures the video tracks that the meeting client is rendering. Which participants are visible depends on the `recording_settings.view` parameter you set when creating the bot:

- **`speaker_view`** (default) — You will receive video for the active speaker only.
- **`gallery_view`** — You will receive video for all participants visible in the gallery.

Screenshare video is always delivered, since the client always renders the screenshare video.

## Capturing Frames at a Single Point in Time

If you only need the per-participant video at a certain moment (for example, the current screenshare when someone in the meeting asks about it), you can request a snapshot over HTTP instead of keeping a websocket connection open for the whole meeting.

Call `POST /api/v1/bots/{bot_id}/per_participant_video_snapshot`:

```json
{
  "webcam_resolution": "none",
  "screenshare_resolution": "1080p"
}
```

`webcam_resolution` and `screenshare_resolution` accept the same values as the websocket settings (`"360p"`, `"720p"`, `"1080p"`, and `"none"` to skip that source). `webcam_resolution` defaults to `"none"` and `screenshare_resolution` defaults to `"360p"`, so only screenshares are captured unless you ask for webcams.

The response contains one frame for each participant video source the bot can currently see:

```json
{
  "frames": [
    {
      "participant_uuid": "16778240",
      "participant_user_uuid": null,
      "participant_name": "Alice",
      "source": "screenshare",
      "frame": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQ...",
      "format": "jpeg",
      "width": 1920,
      "height": 1080,
      "timestamp_ms": 1764177600000
    }
  ]
}
```

The `participant_uuid`, `frame`, `format` and `source` fields mean the same thing as in the websocket message.

Currently snapshots are only supported for Zoom bots using the web SDK (`zoom_settings.sdk` set to `"web"`).

## Code Samples

See [here](https://github.com/attendee-labs/realtime-per-participant-video-and-audio-example) for an example program showing how use this feature.

## Retries on Websocket Connections

Attendee will automatically retry to connect to your websocket server if the connection is lost or the initial connection attempt fails. We will retry up to 30 times with a 2 second delay between retries.

## Error Messages

Currently, we don't give any feedback on errors with the websocket connection. We plan to improve this in the future.
