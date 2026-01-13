<div align="center">
<img src="static/images/logo_black_white.svg" width="300" alt="Attendee Logo">

[![GitHub stars](https://img.shields.io/github/stars/attendee-labs/attendee)](https://github.com/attendee-labs/attendee/stargazers)
[![License](https://img.shields.io/badge/License-Elastic%202.0-blue)](https://github.com/attendee-labs/attendee/blob/main/LICENSE)
[![Slack](https://img.shields.io/badge/Slack-Community-4A154B?logo=slack)](https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g)

**Open source API for meeting bots** — join Zoom, Google Meet, and Teams meetings programmatically.

[Documentation](https://docs.attendee.dev/) · [Try it free](https://app.attendee.dev/accounts/signup/) · [Demo video](https://www.loom.com/embed/b738d02aabf84f489f0bfbadf71605e3?sid=ea605ea9-8961-4cc3-9ba9-10b7dbbb8034)

</div>

---

## What is Attendee?

Attendee is a REST API that handles the infrastructure for meeting bots. You send a meeting URL, and we join a bot that can:

- Capture real-time transcripts
- Record audio and video
- Stream content into meetings (for voice agents)
- Integrate with calendars for scheduled joins

No SDKs to wrangle. No browser automation to maintain. Just HTTP requests.

## Why?

If you've tried building meeting bots, you know each platform is its own world of pain:

| Platform    | Reality                                           |
| ----------- | ------------------------------------------------- |
| Zoom        | C++ SDK, complex auth, platform-specific binaries |
| Google Meet | No SDK—you're running headless Chrome             |
| Teams       | Graph API limitations, enterprise auth complexity |

We've dealt with all of this so you don't have to. One API, three platforms.

## Quick start

**Hosted (easiest)**

1. Sign up at [app.attendee.dev](https://app.attendee.dev/accounts/signup/)
2. Grab your API key from Settings → API Keys
3. Add your credentials (Zoom OAuth + Deepgram)

**Self-hosted**

```bash
docker compose -f dev.docker-compose.yaml build
docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py > .env
# edit .env with your AWS credentials
docker compose -f dev.docker-compose.yaml up
docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full setup.

## Usage

**Join a meeting:**

```bash
curl -X POST https://app.attendee.dev/api/v1/bots \
  -H 'Authorization: Token <YOUR_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "meeting_url": "https://us05web.zoom.us/j/84315220467?pwd=...",
    "bot_name": "My Bot"
  }'
```

```json
{
  "id": "bot_3hfP0PXEsNinIZmh",
  "meeting_url": "https://us05web.zoom.us/j/...",
  "state": "joining",
  "transcription_state": "not_started"
}
```

**Get the transcript:**

```bash
curl https://app.attendee.dev/api/v1/bots/bot_3hfP0PXEsNinIZmh/transcript \
  -H 'Authorization: Token <YOUR_API_KEY>'
```

```json
[
  {
    "speaker_name": "John Doe",
    "timestamp_ms": 1079,
    "duration_ms": 7710,
    "transcription": "Let's discuss the quarterly results..."
  }
]
```

You can poll this during the meeting for real-time transcripts.

Full API reference: [docs.attendee.dev](https://docs.attendee.dev/)

## What people build with this

- Meeting analytics (Gong-style call intelligence)
- Automated note-taking
- Voice agents that participate in meetings
- Compliance monitoring
- Meeting archives

## Features

- **Platforms**: Zoom, Google Meet, Microsoft Teams
- **Transcription**: Real-time, speaker-attributed
- **Recording**: Audio and video capture
- **Webhooks**: State change notifications
- **Calendars**: Auto-join scheduled meetings
- **Voice agents**: Stream audio/video into meetings
- **Self-hostable**: Django app, single Docker image

## Roadmap

- [x] Zoom, Google Meet, Teams support
- [x] Real-time transcripts
- [x] Audio/video recording
- [x] Webhooks
- [x] Calendar integrations
- [x] Voice agents
- [ ] Webex

## Zoom OAuth setup

1. Go to [Zoom Marketplace](https://marketplace.zoom.us/) → Develop → Build App → General App
2. Copy Client ID and Secret from App Credentials
3. Enable Meeting SDK under Embed → Features

[Detailed guide](https://developers.zoom.us/docs/meeting-sdk/developer-accounts/) · [Video walkthrough](https://www.loom.com/embed/7cbd3eab1bc4438fb1badcb3787996d6?sid=825a92b5-51ca-447c-86c1-c45f5294ec9d)

## Contributing

Bug reports, feature requests, and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

Questions? Join us on [Slack](https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g).

---

<div align="center">

[Website](https://attendee.dev/) · [Docs](https://docs.attendee.dev/) · [GitHub](https://github.com/attendee-labs/attendee)

</div>
