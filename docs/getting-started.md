# Getting Started with Attendee

Welcome to Attendee! This tutorial will help you get up and running with meeting bots in minutes, using our hosted instance of Attendee.


<scalar-callout type="neutral">Interested in using Attendee at your company? Schedule a call [here](https://calendly.com/noah-attendee/30min). By self-hosting Attendee you can reduce costs by 10x compared to closed source vendors. You may also check out the guide for [running in development mode](docs/self-hosting) for a preview of self-hosting.</scalar-callout>

## Sign Up

Start with our hosted instance at [app.attendee.dev](https://app.attendee.dev/accounts/signup/). Create an account, and follow the instructions on the **Quick start** section in the sidebar.

## Create an API key

Sign in to your Attendee account and navigate to the "API Keys" section in the sidebar. Once there, you can create a new API key by giving it a name.

## Configure Your Credentials

You'll need to set up credentials for the platforms you want to use. Enter these credentials in the "Settings" section of your Attendee dashboard.

### For Zoom Meetings:
- **Zoom OAuth Credentials**: You'll need these to join Zoom meetings. These are the Zoom app Client ID and secret that uniquely identify your bot.
    1. Navigate to [Zoom Marketplace](https://marketplace.zoom.us/) and register/log into your developer account.
    2. Click the "Develop" button at the top-right, then click 'Build App' and choose "General App".
    3. Copy the Client ID and Client Secret from the 'App Credentials' section
    4. Go to the Embed tab on the left navigation bar under Features, then select the Meeting SDK toggle.
- **Deepgram API Key**: You'll need a Deepgram API key for transcribing Zoom meetings. You can sign up for an account here, no credit card required and get 400 hours worth of free transcription. Sign up at [Deepgram](https://console.deepgram.com/signup) for transcription (400 free hours included)

For more details, follow [this guide from Zoom](https://developers.zoom.us/docs/meeting-sdk/developer-accounts/) or watch the video below.
<scalar-embed
  src="https://www.loom.com/embed/7cbd3eab1bc4438fb1badcb3787996d6?sid=825a92b5-51ca-447c-86c1-c45f5294ec9d"
  caption="Setting Up API Calls for Attendee App"
  alt="Interactive demonstration of getting started with Attendee">
</scalar-embed>

### For Google Meet / Microsoft Teams:
- No additional credentials needed for basic functionality

## Join Your First Meeting

<scalar-steps>
<scalar-step id="step-0" title="Start a meeting">
First, start a meeting in your preferred platform. We'll use Zoom for this example. Copy the meeting URL. You'll need this in the next step.
</scalar-step>

<scalar-step id="step-1" title="Create a bot to join your meeting">
With your Attendee API key, send a POST request to join your current meeting:

```bash
curl -X POST https://app.attendee.dev/api/v1/bots \
-H 'Authorization: Token <YOUR_API_KEY>' \
-H 'Content-Type: application/json' \
-d '{"meeting_url": "https://us05web.zoom.us/j/84315220467?pwd=9M1SQg2Pu2l0cB078uz6AHeWelSK19.1", "bot_name": "My Bot"}'
```

The API will respond with an object that represents your bot's state in the meeting.:
```json
{
  "id":"bot_3hfP0PXEsNinIZmh",
  "meeting_url":"https://us05web.zoom.us/j/4849920355?pwd=aTBpNz760UTEBwUT2mQFtdXbl3SS3i.1",
  "state":"joining",
  "transcription_state":"not_started"
}
```
</scalar-step>
</scalar-steps>

## Monitor Your Bot

<scalar-steps>
<scalar-step id="step-1" title="Get the bot's status">
Send a GET request to poll the bot:

```bash
curl -X GET https://app.attendee.dev/api/v1/bots/bot_3hfP0PXEsNinIZmh \
-H 'Authorization: Token <YOUR_API_KEY>' \
-H 'Content-Type: application/json'
```

Response:
```json
{
  "id":"bot_3hfP0PXEsNinIZmh",
  "meeting_url":"https://us05web.zoom.us/j/88669088234?pwd=AheaMumvS4qxh6UuDtSOYTpnQ1ZbAS.1",
  "state":"ended",
  "transcription_state":"complete"
}
```
<scalar-callout type="info">When the endpoint returns a state of `ended`, it means the meeting has ended. When the `transcription_state` is `complete` it means the meeting recording has been transcribed.</scalar-callout>
</scalar-step>
<scalar-step id="step-2" title="Retrieve the meeting transcripts">
Once the meeting has ended and the transcript is ready, make a GET request to `/bots/<id>/transcript` to retrieve the meeting transcripts:

```bash
curl -X GET https://app.attendee.dev/api/v1/bots/bot_3hfP0PXEsNinIZmh/transcript \
-H 'Authorization: Token mpc67dedUlzEDXfNGZKyC30t6cA11TYh' \
-H 'Content-Type: application/json'
```

Response:
```json
[{
  "speaker_name":"Noah Duncan",
  "speaker_uuid":"16778240",
  "speaker_user_uuid":"AAB6E21A-6B36-EA95-58EC-5AF42CD48AF8",
  "timestamp_ms":1079,
  "duration_ms":7710,
  "transcription":"You can totally record this, buddy. You can totally record this. Go for it, man."
},...]
```

<scalar-callout type="info">You can also query this endpoint while the meeting is happening to retrieve partial transcripts.
</scalar-callout>

</scalar-step>
</scalar-steps>

You can also watch this video below which demos the basic API requests.
<scalar-embed
  src="https://www.loom.com/embed/b738d02aabf84f489f0bfbadf71605e3?sid=ea605ea9-8961-4cc3-9ba9-10b7dbbb8034"
  caption="API Requests Tutorial"
  alt="Interactive demonstration of Attendee API">
</scalar-embed>

## Next Steps

<scalar-page-link filepath="docs/basics.md" title="Basics of Bots" description="Read the basics about bots, what they do, and the bot states">
</scalar-page-link>

<scalar-page-link filepath="docs/faq.md" title="FAQ" description="For troubleshooting, check out our FAQ page for solutions to common problems">
</scalar-page-link>

If you are interested in a self-hosted Attendee solution, try running Attendee in development mode in the next page of the guide.