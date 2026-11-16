# Participant Events

Attendee tracks all participants in a meeting and when they take certain actions. This information can be used for tracking meeting attendance or triggering actions when a certain number of participants have joined. It can also be used to track when participants are speaking.

The bot itself is not considered a participant in the meeting and will not appear in the participant events.

## Participant Event Types

Currently, there are ten types of participant events:

- **Join**: A participant has joined the meeting.
- **Leave**: A participant has left the meeting.
- **Speech Start**: A participant has started speaking.
- **Speech Stop**: A participant has stopped speaking.
- **Screenshare Start**: A participant has started sharing their screen.
- **Screenshare Stop**: A participant has stopped sharing their screen.
- **Unmute**: A participant has unmuted their microphone.
- **Mute**: A participant has muted their microphone.
- **Webcam On**: A participant has turned on their webcam.
- **Webcam Off**: A participant has turned off their webcam.

> **Note:** Screenshare start/stop events are supported on Zoom, Google Meet, and Microsoft Teams, but not Zoom RTMS.

> **Note:** Participants are assumed to be muted with their webcam off when the bot first sees them, so no mute/unmute or webcam events are sent for a participant who stays muted with their webcam off. If a participant is already unmuted or has their webcam on when the bot first sees them, an unmute or webcam on event is sent at that time. When an unmuted participant or a participant with their webcam on leaves, a mute or webcam off event is sent before the leave event. Mute/unmute and webcam on/off events are supported on Zoom, Google Meet, and Microsoft Teams, but not Zoom RTMS or Webex.

## Fetching Participant Events

You can retrieve a list of participant events for a specific bot by making a GET request to the `/bots/{bot_id}/participant_events` endpoint.

For more details on the API, see the [API reference](https://docs.attendee.dev/api-reference/tag/bots/get/api/v1/bots/object_id/participant_events).

## Webhooks for Participant Events

You can also receive real-time notifications for participant events by setting up a webhook. For participant join/leave events, create a webhook in the dashboard and ensure the `participant_events.join_leave` trigger is enabled. For participant speech start/stop events, create a webhook in the dashboard and ensure the `participant_events.speech_start_stop` trigger is enabled. For participant screenshare start/stop events, create a webhook in the dashboard and ensure the `participant_events.screenshare_start_stop` trigger is enabled. For participant mute/unmute events, ensure the `participant_events.mute_unmute` trigger is enabled. For participant webcam on/off events, ensure the `participant_events.webcam_on_off` trigger is enabled.

When a participant joins or leaves or starts or stops speaking or starts or stops sharing their screen or mutes or unmutes or turns their webcam on or off, Attendee will send a webhook payload to your specified URL. For more details on the webhook payload, see the [webhooks documentation](https://docs.attendee.dev/guides/webhooks#payload-for-participant-event-triggers).

