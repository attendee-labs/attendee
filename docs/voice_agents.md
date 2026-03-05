# Voice Agents

Attendee supports bringing **voice agents that run in a webpage** directly into your meeting. Simply provide a URL to your voice agent web page, and Attendee will:

- Capture the audio and video output from the page
- Stream that audio and video into the meeting
- Feed the meeting audio into the page as the "microphone" input

This makes it easy to integrate AI agents that work with both audio and video.

::scalar-embed{ src="https://www.youtube.com/watch?v=U2j3oCYv488" caption="Voice Agents in Attendee"}

## Advantages of loading a webpage containing a voice agent

While Attendee also supports voice agents via passing audio packets over [websockets](https://docs.attendee.dev/guides/realtime-audio-input-and-output), loading a webpage offers several advantages:

1. **Video support**  
   You can stream an avatar for the voice agent, in addition to audio.

2. **Easy development workflow**  
   You can test your voice agent directly in the browser without involving Attendee. When it works in the browser, just supply the same URL to Attendee and it will behave the same way inside a meeting.

3. **No backend worker required**  
  With WebSocket audio, you must provide a backend service to handle sending and receiving audio packets. With voice agents, Attendee runs your webpage inside an Attendee-managed container, effectively acting as that backend.

## Calling the API

To add a voice agent, supply a URL in the `voice_agent_settings` parameter when creating a bot:

```json
{
  "meeting_url": "https://meet.google.com/abc-def-ghi",
  "bot_name": "Avatar Bot",
  "voice_agent_settings": {
    "url": "https://your-voice-agent-app.com?agent_id=1234567890"
  }
}
```

The agent will be loaded once the bot joins the meeting and starts recording.

## Screen sharing

You can share a screen alongside your voice agent by adding `screenshare_url` to `voice_agent_settings`.


```json
{
  "meeting_url": "https://meet.google.com/abc-def-ghi",
  "bot_name": "Avatar Bot",
  "voice_agent_settings": {
    "url": "https://your-voice-agent-app.com?agent_id=1234567890",
    "screenshare_url": "https://your-app.com/screen"
  }
}
```
The URL should point to a publicly accessible page that renders the content you want to share (e.g. a live browser view, dashboard, or canvas). Attendee will capture and stream that page as the bot's screen share in the meeting.

## Setting up your webpage to be loaded by Attendee

In order to bring your voice agent into a meeting, Attendee will launch a container that loads the url you provided and streams its audio and video to the meeting. In order for that process to work, follow these guidelines:

1. Your webpage must be publicly accessible and work via HTTPS.

2. Your webpage should immediately ask the user for permission to use the microphone. The Attendee container will grant the microphone permission automatically. The webpage must not require the user to click a button to start the call. 

3. The Attendee container will load your webpage with screen dimensions of 1280x720, so your webpage should be designed for display at that size.

4. Pass any references to objects in your system (IE agent_id) via query parameters in the webpage URL.

For an example of a simple website that loads a VAPI voice agent, view the source of [this example page](https://attendee.dev/vapi_voice_agent_example).

## Quick start

This guide shows how to set up a VAPI voice agent that can be loaded by Attendee.

1. Go to the [VAPI website](https://vapi.ai) and create a free account.
2. Get your [public key](https://dashboard.vapi.ai/org/api-keys) and [assistant id](https://dashboard.vapi.ai/assistants/) in the VAPI dashboard.
3. Call the Attendee API with the following payload:

```bash
curl -X POST https://app.attendee.dev/api/v1/bots \
  -H 'Authorization: Token YOUR_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "meeting_url": "https://meet.google.com/abc-def-ghi",
    "bot_name": "Vapi Agent",
    "voice_agent_settings": {
      "url": "https://attendee.dev/vapi_voice_agent_example?assistant_id=YOUR_ASSISTANT_ID&public_key=YOUR_PUBLIC_KEY"
    },
  }'
```

4. The agent will be loaded once the bot joins the meeting and starts recording. It will ask you to book an appointment at a healthcare provider.

For a similar example using Tavus to render a photorealistic avatar, see [here](https://attendee.dev/tavus_voice_agent_example).