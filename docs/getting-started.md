# Getting Started with Attendee

Welcome to Attendee! This guide will help you get up and running with meeting bots in minutes.

## What is Attendee?

Attendee is an open-source API that makes it easy to create and manage meeting bots for platforms like Zoom, Google Meet, and Microsoft Teams. With Attendee, you can:

- **Record meetings** - Capture audio and video from virtual meetings
- **Transcribe conversations** - Get real-time transcription with speaker identification
- **Send chat messages** - Have your bot participate in meeting chats
- **Speak into meetings** - Have your bot speak arbitrary audio into the meeting

This tutorial will go through the following steps to get you started with Attendee:

1. **Sign up** for free at [app.attendee.dev](https://app.attendee.dev/accounts/signup/)
2. **Get your API key** from the "API Keys" section in your dashboard
3. **Configure credentials** for Zoom, Google Meet, or Teams in the "Settings" section
4. **Join a meeting** with a simple API call
5. **Monitor your bot** and retrieve transcripts when the meeting ends

## Sign Up

Start with our hosted instance at [app.attendee.dev](https://app.attendee.dev/accounts/signup/) - no credit card required!

## Get Your API Key

1. Sign in to your Attendee account
2. Navigate to the "API Keys" section in the sidebar
3. Create a new API key for your application

## Configure Your Credentials

You'll need to set up credentials for the platforms you want to use:

### For Zoom Meetings:
- **Zoom OAuth Credentials**: Needed to join Zoom meetings. These are the Zoom app client id and secret that uniquely identify your bot.
    1. Navigate to [Zoom Marketplace](https://marketplace.zoom.us/) and register/log into your developer account.
    2. Click the "Develop" button at the top-right, then click 'Build App' and choose "General App".
    3. Copy the Client ID and Client Secret from the 'App Credentials' section
    4. Go to the Embed tab on the left navigation bar under Features, then select the Meeting SDK toggle.
- **Deepgram API Key**: Needed for transcribing Zoom meetings. You can sign up for an account here, no credit card required and get 400 hours worth of free transcription. Sign up at [Deepgram](https://console.deepgram.com/signup) for transcription (400 free hours included)

For more details, follow [this guide](https://developers.zoom.us/docs/meeting-sdk/developer-accounts/) or watch [this video](https://www.loom.com/embed/7cbd3eab1bc4438fb1badcb3787996d6?sid=825a92b5-51ca-447c-86c1-c45f5294ec9d).

### For Google Meet:
- No additional credentials needed for basic functionality

### For Microsoft Teams:
- No additional credentials needed for basic functionality

Enter these credentials in the "Settings" section of your Attendee dashboard.

## Join Your First Meeting

Make a simple API call to join a meeting:

```bash
curl -X POST https://app.attendee.dev/api/v1/bots \
-H 'Authorization: Token <YOUR_API_KEY>' \
-H 'Content-Type: application/json' \
-d '{"meeting_url": "https://us05web.zoom.us/j/84315220467?pwd=9M1SQg2Pu2l0cB078uz6AHeWelSK19.1", "bot_name": "My Bot"}'
```

Response:
```json
{
  "id":"bot_3hfP0PXEsNinIZmh",
  "meeting_url":"https://us05web.zoom.us/j/4849920355?pwd=aTBpNz760UTEBwUT2mQFtdXbl3SS3i.1",
  "state":"joining",
  "transcription_state":"not_started"
}
```

## Monitor Your Bot

Check your bot's status:

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

When the endpoint returns a state of `ended`, it means the meeting has ended. When the `transcription_state` is `complete` it means the meeting recording has been transcribed.

### Get Your Transcript

Once the meeting has ended and the transcript is ready make a GET request to `/bots/<id>/transcript` to retrieve the meeting transcripts:

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

You can also query this endpoint while the meeting is happening to retrieve partial transcripts.

## Next Steps

- **Learn the basics**: Check out our [Basics guide](./basics) for detailed information about bot capabilities
- **Set up webhooks**: Configure [webhooks](./webhooks) to get real-time updates about your bots
- **Schedule bots**: Learn how to [schedule bots](./scheduled_bots) for recurring meetings
- **Integrate with calendars**: Set up [calendar integration](./calendar_integration) for automatic meeting detection
- **Explore the API**: View our complete [API documentation](/api-reference)

## Self-Hosting

Want to run Attendee on your own infrastructure? Attendee is designed for easy self-hosting and can reduce costs by 10x compared to closed-source alternatives. Check out our [self-hosting guide](../README.md#self-hosting) for instructions.

## Need Help?

- **Documentation**: Browse our comprehensive guides
- **Community**: Join our [Slack community](https://join.slack.com/t/attendeecommu-rff8300/shared_invite/zt-2uhpam6p2-ZzLAoVrljbL2UEjqdSHrgQ)
- **Support**: Schedule a call with our team at [calendly.com/noah-attendee/30min](https://calendly.com/noah-attendee/30min)
- **Issues**: Report bugs or request features on [GitHub](https://github.com/attendee-labs/attendee)

## Common Issues

Having trouble? Check our [FAQ](./faq) for solutions to common problems, including:
- Setting up Zoom OAuth credentials
- Troubleshooting bot joining issues
- Resolving recording problems
- Local development setup issues
