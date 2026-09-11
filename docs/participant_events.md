# Participant Events

Attendee tracks all participants in a meeting and when they take certain actions. This information can be used for tracking meeting attendance or triggering actions when a certain number of participants have joined. It can also be used to track when participants are speaking or sharing their screen.

The bot itself is not considered a participant in the meeting and will not appear in the participant events.

## Participant Event Types

Currently, there are six types of participant events:

- **Join**: A participant has joined the meeting.
- **Leave**: A participant has left the meeting.
- **Speech Start**: A participant has started speaking.
- **Speech Stop**: A participant has stopped speaking.
- **Screenshare Start**: A participant has started sharing their screen.
- **Screenshare Stop**: A participant has stopped sharing their screen.

Speech and screenshare events are opt-in. Enable them in `recording_settings` when creating the bot:

```json
{
  "meeting_url": "https://zoom.us/j/123456789",
  "bot_name": "My Bot",
  "recording_settings": {
    "record_participant_speech_start_stop_events": true,
    "record_participant_screenshare_start_stop_events": true
  }
}
```

Both default to `false`; screenshare events contain an empty `event_data` object and do not require realtime video streaming.

## Screenshare sessions

A start means the bot first observed a participant sharing, including a share already in progress when it joined; a stop means the participant stopped their last observed share or left. Native Zoom and Zoom Web keep paused shares in the same session, and changing the viewed share does not stop another participant's session. Timestamps are the bot's observation time, not the original start time of a share discovered on join. These events describe sharing state, not whether a particular frame reached a consumer, and disconnecting the bot does not synthesize a stop for every participant.

| Adapter | Signal | Support |
| --- | --- | --- |
| Google Meet | Presentation devices linked to the sharing participant | Implemented |
| Microsoft Teams | Application-sharing streams in roster updates and active-call snapshots | Implemented |
| Zoom native SDK | Sharing callbacks and the initial sharing-source list | Implemented |
| Zoom Web | Participant sharing flags in the pinned Meeting SDK 5.1.4 roster | Implemented; depends on SDK internals |
| Zoom RTMS | Sharing signaling events | Not implemented |

Zoom RTMS's documented sharing-stop event has no participant ID, and sharing events require the `DESKSHARE` scope; participant attribution and joining during an existing share need separate validation before support is added. See [Zoom RTMS events](https://developers.zoom.us/docs/rtms/event-reference/).

## Fetching Participant Events

You can retrieve a list of participant events for a specific bot by making a GET request to the `/bots/{bot_id}/participant_events` endpoint. Speech and screenshare events are only returned when enabled.

For more details on the API, see the [API reference](https://docs.attendee.dev/api-reference/tag/bots/get/api/v1/bots/object_id/participant_events).

## Webhooks for Participant Events

You can also receive real-time notifications for participant events by setting up a webhook. For participant join/leave events, create a webhook in the dashboard and ensure the `participant_events.join_leave` trigger is enabled. For participant speech start/stop events, create a webhook in the dashboard and ensure the `participant_events.speech_start_stop` trigger is enabled. For participant screenshare start/stop events, create a webhook in the dashboard and ensure the `participant_events.screenshare_start_stop` trigger is enabled.

When a participant joins or leaves, starts or stops speaking, or starts or stops sharing their screen, Attendee will send a webhook payload to your specified URL. For more details on the webhook payload, see the [webhooks documentation](https://docs.attendee.dev/guides/webhooks#payload-for-participantevents.joinleave-participantevents.speechstartstop-and-participantevents.screensharestartstop).

