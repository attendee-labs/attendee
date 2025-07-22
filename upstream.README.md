<div align="center">
<img src="static/images/logo_black_white.svg" width="300" alt="Attendee Logo">
</div>
<h2 align="center">Meeting bots made easy</h2>
<p align="center">
    <a href="https://docs.attendee.dev/">Documentation</a>
    ·
    <a href="https://attendee.dev/">Website</a>
    ·
    <a href="https://join.slack.com/t/attendeecommu-rff8300/shared_invite/zt-2uhpam6p2-ZzLAoVrljbL2UEjqdSHrgQ">Slack</a>
</p>


Attendee is an open source API for managing meeting bots on platforms like Zoom or Google Meet. Bring meeting transcripts and recordings into your product in days instead of months. 

See a [quick demo of the API](https://www.loom.com/embed/b738d02aabf84f489f0bfbadf71605e3?sid=ea605ea9-8961-4cc3-9ba9-10b7dbbb8034).

## Getting started

Sign up for free on our hosted instance [here](https://app.attendee.dev/accounts/signup/).
 
## Self hosting

Attendee is designed for convenient self-hosting. It runs as a Django app in a single Docker image. The only external services needed are Postgres and Redis. Directions for running locally in development mode [here](#running-in-development-mode).

## Why use Attendee?

Meeting bots are powerful because they have access to the same audio and video streams as human users of meeting software. They power software like Gong or Otter.ai.

Building meeting bots is challenging across all platforms, though some have more support than others. Zoom provides a powerful [SDK](https://developers.zoom.us/docs/meeting-sdk/), but it is low-level and advanced features like per-participant audio streams are only available in the C++ variants of the SDK. Google Meet doesn't provide any support at all, so you need to run a full instance of Google Meet in Chrome.

Attendee abstracts away this complexity into a single developer friendly REST API that manages the state and media streams from these bots. If you're a developer building functionality that requires meeting bots, Attendee can save you months of work vs building from scratch.

## Calling the API

Join a meeting with a POST request to `/bots`:
```
curl -X POST https://app.attendee.dev/api/v1/bots \
-H 'Authorization: Token <YOUR_API_KEY>' \
-H 'Content-Type: application/json' \
-d '{"meeting_url": "https://us05web.zoom.us/j/84315220467?pwd=9M1SQg2Pu2l0cB078uz6AHeWelSK19.1", "bot_name": "My Bot"}'
```
Response:
```{"id":"bot_3hfP0PXEsNinIZmh","meeting_url":"https://us05web.zoom.us/j/4849920355?pwd=aTBpNz760UTEBwUT2mQFtdXbl3SS3i.1","state":"joining","transcription_state":"not_started"}```

The API will respond with an object that represents your bot's state in the meeting. 



Make a GET request to `/bots/<id>` to poll the bot:
```
curl -X GET https://app.attendee.dev/api/v1/bots/bot_3hfP0PXEsNinIZmh \
-H 'Authorization: Token <YOUR_API_KEY>' \
-H 'Content-Type: application/json'
```
Response: 
```{"id":"bot_3hfP0PXEsNinIZmh","meeting_url":"https://us05web.zoom.us/j/88669088234?pwd=AheaMumvS4qxh6UuDtSOYTpnQ1ZbAS.1","state":"ended","transcription_state":"complete"}```

When the endpoint returns a state of `ended`, it means the meeting has ended. When the `transcription_state` is `complete` it means the meeting recording has been transcribed.


Once the meeting has ended and the transcript is ready make a GET request to `/bots/<id>/transcript` to retrieve the meeting transcripts:
```
curl -X GET https://app.attendee.dev/api/v1/bots/bot_3hfP0PXEsNinIZmh/transcript \
-H 'Authorization: Token mpc67dedUlzEDXfNGZKyC30t6cA11TYh' \
-H 'Content-Type: application/json'
```
Response:
```
[{
"speaker_name":"Noah Duncan",
"speaker_uuid":"16778240","speaker_user_uuid":"AAB6E21A-6B36-EA95-58EC-5AF42CD48AF8",
"timestamp_ms":1079,"duration_ms":7710,
"transcription":"You can totally record this, buddy. You can totally record this. Go for it, man."
},...]
```
You can also query this endpoint while the meeting is happening to retrieve partial transcripts.

## Prerequisites

To call the API you need the following

1. Attendee API Key - These are created in the Attendee UI by creating an account in your Attendee instance, signing in and navigating to the 'API Keys' section in the sidebar.

2. Zoom OAuth Credentials - These are the Zoom app client id and secret that uniquely identify your bot. We need these to join meetings. Directions on obtaining them [here](#obtaining-zoom-oauth-credentials).

3. Deepgram API Key - We use Deepgram to generate recording transcripts. You can sign up for a free account [here](https://console.deepgram.com/signup), no credit card required.

The Zoom OAuth credentials and Deepgram API key are entered into the Attendee UI in the 'Settings' section in the sidebar.

## Missing feature?

Attendee is in very early beta, but functionality is being added rapidly. If the API is missing something you need, then open an issue in the repository. PRs are also welcome!

## Obtaining Zoom OAuth Credentials

- Navigate to [Zoom Marketplace](https://marketplace.zoom.us/) and register/log into your
developer account.
- Click the "Develop" button at the top-right, then click 'Build App' and choose "General App".
- Copy the Client ID and Client Secret from the 'App Credentials' section
- Go to the Embed tab on the left navigation bar under Features, then select the Meeting SDK toggle.

For more details, follow [this guide](https://developers.zoom.us/docs/meeting-sdk/developer-accounts/) or watch this [video](https://www.loom.com/embed/7cbd3eab1bc4438fb1badcb3787996d6?sid=825a92b5-51ca-447c-86c1-c45f5294ec9d).

## Running in development mode

- Build the Docker image: `docker compose -f dev.docker-compose.yaml build` (Takes about 5 minutes)
- Create local environment variables: `docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py > .env`
- Edit the `.env` file and enter your AWS information.
- Start all the services: `docker compose -f dev.docker-compose.yaml up`
- After the services have started, run migrations in a separate terminal tab: `docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate`
- Goto localhost:8000 in your browser and create an account
- The confirmation link will be written to the server logs in the terminal where you ran `docker compose -f dev.docker-compose.yaml up`. Should look like `http://localhost:8000/accounts/confirm-email/<key>/`.
- Paste the link into your browser to confirm your account.
- You should now be able to log in, input your credentials and obtain an API key. API calls should be directed to http://localhost:8000 instead of https://app.attendee.dev.


## Contribute 

Attendee is open source. The best way to contribute is to open an issue or join the [Slack Community](https://join.slack.com/t/attendeecommu-rff8300/shared_invite/zt-2uhpam6p2-ZzLAoVrljbL2UEjqdSHrgQ) and let us know what you want to build.

See CONTRIBUTING.md for detailed instructions on how to contribute to Attendee.


## Roadmap

- [x] Join and leave Zoom meetings
- [x] Transcripts
- [x] API Reference
- [x] Audio input / output
- [x] Video input / output
- [x] Google Meet support
- [x] Speech support
- [x] Automatically leave meetings
- [x] Microsoft Teams support
- [x] Webhooks for state changes
- [ ] Audio input / output via websockets
- [ ] [ZAK token](https://developers.zoom.us/docs/meeting-sdk/auth/#start-meetings-and-webinars-with-a-zoom-users-zak-token) and [Join token](https://developers.zoom.us/docs/api/meetings/#tag/meetings/GET/meetings/{meetingId}/jointoken/local_recording) support
- [ ] Scheduled meetings

Have suggestions for the roadmap? Join the [Slack Community](https://join.slack.com/t/attendeecommu-rff8300/shared_invite/zt-2uhpam6p2-ZzLAoVrljbL2UEjqdSHrgQ) or open an issue.
