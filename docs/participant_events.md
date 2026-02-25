# Participant Events

Attendee tracks all participants in a meeting and when they take certain actions. This information can be used for tracking meeting attendance or triggering actions when a certain number of participants have joined. It can also be used to track when participants are speaking.

The bot itself is not considered a participant in the meeting and will not appear in the participant events.

## Participant Event Types

Currently, there are six types of participant events:

- **Join**: A participant has joined the meeting.
- **Leave**: A participant has left the meeting.
- **Speech Start**: A participant has started speaking.
- **Speech Stop**: A participant has stopped speaking.
- **Screenshare Start**: A participant has started sharing their screen.
- **Screenshare Stop**: A participant has stopped sharing their screen.

> **Note:** Screenshare start/stop events are supported on Zoom (native SDK), Google Meet, and Microsoft Teams. They are not currently supported on Zoom Web or Zoom RTMS.

## Fetching Participant Events

You can retrieve a list of participant events for a specific bot by making a GET request to the `/bots/{bot_id}/participant_events` endpoint.

For more details on the API, see the [API reference](https://docs.attendee.dev/api-reference#tag/bots/get/api/v1/bots/{object_id}/participant_events).

## Webhooks for Participant Events

You can also receive real-time notifications for participant events by setting up a webhook. For participant join/leave events, create a webhook in the dashboard and ensure the `participant_events.join_leave` trigger is enabled. For participant speech start/stop events, create a webhook in the dashboard and ensure the `participant_events.speech_start_stop` trigger is enabled. For participant screenshare start/stop events, create a webhook in the dashboard and ensure the `participant_events.screenshare_start_stop` trigger is enabled.

When a participant joins or leaves, starts or stops speaking, or starts or stops sharing their screen, Attendee will send a webhook payload to your specified URL. For more details on the webhook payload, see the [webhooks documentation](https://docs.attendee.dev/guides/webhooks#webhook-payload__payload-for-participanteventsjoinleave-and-participanteventsspeechstartstop-triggers).

