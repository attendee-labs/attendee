# Attendee-managed Zoom RTMS

Zoom [Realtime Media Streams (RTMS)](https://developers.zoom.us/docs/rtms/) is a Zoom-native data pipeline that gives your app access to live audio, video, transcript, and screenshare data from Zoom meetings. Unlike meeting bots which join as visible participants, RTMS streams meeting data directly to your application without adding anyone to the call.

Attendee provides support for RTMS through a new API entity called **App Sessions**. When a user activates an RTMS app during a meeting, Zoom sends a webhook to your application, and you forward that payload to Attendee to create an app session. Attendee then handles connecting to the RTMS stream, processing the media, and delivering transcripts and recordings.

For reference implementations, see the example programs for building a [notetaker](https://github.com/attendee-labs/rtms-notetaker-example) and [sales coach](https://github.com/attendee-labs/rtms-sales-coach-example) with Attendee and RTMS. For quick demo of an RTMS app built with Attendee, see the video [here](https://www.youtube.com/watch?v=56DzvzJHSv4).

## RTMS vs Bots

There are two key differences between RTMS and bots:

**No bot participant in the meeting.** RTMS does not add a participant to the call. The meeting data is streamed to your app through Zoom's infrastructure, so there is no "bot has joined" notification and no extra attendee in the participant list.

**The user controls when your app connects to the meeting.** With a bot, you are in control of when the bot attempts to join the meeting — you make an API call and Attendee sends the bot in. With RTMS, the user is in control. When the user opens your RTMS app, Zoom sends your app a webhook that it must respond to. The user can also pause the RTMS app's recording at any time.

Other advantages of RTMS:

- **No onbehalf token required.** RTMS is not affected by Zoom's [March 2, 2026 deadline](https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/) requiring onbehalf tokens for Meeting SDK bots joining external meetings. You also do not need to implement join tokens or any OAuth flow logic in your app.
- **Less CPU usage.** RTMS sends encoded video frames, which is less CPU-intensive to process than the raw video frames sent when using the Zoom Meeting SDK.

Limitations of RTMS:

- **Receive data only.** RTMS cannot send data back into the meeting. If you need your app to post messages to the meeting chat, or send video and audio into the meeting, you'll need a bot.

## How to implement RTMS with Attendee

### Create an RTMS App in the Zoom Developer Portal

1. Go to the [Zoom Developer Portal](https://marketplace.zoom.us/user/build) and create a new General app.

2. On the sidebar select 'Basic Information'.
3. For the OAuth redirect URLs, you can write https://zoom.us or any other URL, assuming you app does not need to use OAuth.

4. On the sidebar select 'Access'.
5. Click 'Add new Event Subscription'.
6. Subscribe to the 'RTMS started' and 'RTMS stopped' events.
7. Set the 'Event notification endpoint URL' to an endpoint on your application for handling Zoom webhooks.
8. Save the changes.

9. On the sidebar select 'Scopes'.
10. Add the following scopes:
    - meeting:read:meeting_audio
    - meeting:read:meeting_transcript
    - meeting:read:meeting_chat
    - meeting:read:meeting_video
    - meeting:read:meeting_screenshare

11. On the sidebar select 'Local test'.
12. Click the 'Add app now' button and authorize the app.

13. Go to your Zoom App Settings at https://zoom.us/profile/setting?tab=zoomapps
14. Enable share realtime meeting content with apps
15. Under "Auto-start apps that access shared realtime meeting content" click the "Choose an app to auto-start" button and select your app.

### Register your RTMS App with Attendee

1. Go to the Attendee dashboard and create a new project for your RTMS app.
2. Navigate to **Settings → Credentials**
2. Under Zoom OAuth App Credentials, click **"Add OAuth App"**
3. Enter your Zoom Client ID, Client Secret from your RTMS App
4. Click **"Save"**

### Configure webhooks in Attendee

1. Go to **Settings -> Webhooks**.
2. Click on 'Create Webhook' and select whichever webhook triggers you want to receive from Attendee. You will most likely want to subscribe to the `bot.stage_change` trigger, which despite the name, is fired when the rtms session changes state. The webhook destination url should be a different url than the one you used for the Zoom webhook endpoint, because these webhooks are coming from Attendee, not Zoom.
3. Click **"Create"** to save your webhook.

### Add code to your application to handle the meeting.rtms_started webhook from Zoom

This code will need to handle the `meeting.rtms_started` webhook from Zoom and forward the webhook payload to Attendee by calling `POST /api/v1/app_sessions`. Set the `zoom_rtms` field equal to the payload from the `meeting.rtms_started` webhook. You can also specify the same settings for metadata, transcription and recording that you can for a bot. See [here](https://github.com/attendee-labs/rtms-notetaker-example/blob/d51d7f79d13151ffa97369bf264736f244fe35e4/index.js#L70) for an example.


### Add code to your application to handle the bot.stage_change webhook from Attendee

The `bot.state_change` webhook is fired when the rtms session changes state. You will want to look for the `ended` state, which means that the rtms session has ended and you can retrieve the recording and transcript from the app session API endpoints. See [here](https://github.com/attendee-labs/rtms-notetaker-example/blob/d51d7f79d13151ffa97369bf264736f244fe35e4/index.js#L119) for an example.

## Other App session API endpoints

**`GET /api/v1/app_sessions/{id}/media`**

Returns the recording and media files for a completed app session. Only available after the session has moved to the `ended` state.

### Get App Session Transcript

**`GET /api/v1/app_sessions/{id}/transcript`**

Returns the full transcript for a completed app session.

### Get App Session Participant Events

**`GET /api/v1/app_sessions/{id}/participant_events`**

Returns participant join/leave events for the app session.

## FAQ

### Does RTMS require the On Behalf Of (OBF) token?

No. RTMS is a separate integration path from the Meeting SDK and is not affected by Zoom's [March 2, 2026 OBF token deadline](https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/). If you switch your Zoom integration from bots to RTMS, you do not need to implement the On Behalf Of (OBF) token or any of the other Zoom tokens used in the Meeting SDK.

### What happens if the host doesn't have RTMS enabled?

Your app will not receive the `meeting.rtms_started` webhook and no data will be captured. RTMS requires the meeting host's Zoom account to have the feature enabled and your app authorized. This is a key consideration if your users join meetings hosted by people outside your organization.