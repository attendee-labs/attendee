<div align="center">
<img src="static/images/logo_black_white.svg" width="300" alt="Attendee Logo">
</div>

<h2 align="center">Meeting bots made easy</h2>

<p align="center">
  <a href="https://github.com/attendee-labs/attendee/stargazers">
    <img src="https://img.shields.io/github/stars/attendee-labs/attendee?style=for-the-badge" alt="GitHub stars">
  </a>
  <a href="https://github.com/attendee-labs/attendee/issues">
    <img src="https://img.shields.io/github/issues/attendee-labs/attendee?style=for-the-badge" alt="GitHub issues">
  </a>
  <a href="https://github.com/attendee-labs/attendee/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/License-Elastic%202.0-blue?style=for-the-badge" alt="License">
  </a>
  <a href="https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g">
    <img src="https://img.shields.io/badge/Slack-Join%20Community-4A154B?style=for-the-badge&logo=slack" alt="Slack">
  </a>
</p>

<p align="center">
    <a href="https://docs.attendee.dev/">📚 Documentation</a>
    ·
    <a href="https://attendee.dev/">🌐 Website</a>
    ·
    <a href="https://app.attendee.dev/accounts/signup/">🚀 Try Free</a>
</p>

---

> **Build meeting bots in days, not months.** Attendee is an open source API that lets you join Zoom, Google Meet, and Teams meetings, capture transcripts, record audio/video, and build powerful meeting automation—all through a simple REST API.

**🎥 [Watch Demo](https://www.loom.com/embed/b738d02aabf84f489f0bfbadf71605e3?sid=ea605ea9-8961-4cc3-9ba9-10b7dbbb8034)** | **📖 [Read Docs](https://docs.attendee.dev/)** | **💬 [Join Slack](https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g)**

---

## 📋 Table of Contents

- [Why Attendee?](#-why-attendee)
- [Use Cases](#-use-cases)
- [Key Features](#-key-features)
- [Quick Start](#-quick-start)
- [API Examples](#-api-examples)
- [Self Hosting](#-self-hosting)
- [Contributing](#-contributing)
- [Roadmap](#-roadmap)

## 🎯 Why Attendee?

Building meeting bots is **hard**. Each platform has different SDKs, APIs, and limitations:

- **Zoom**: Low-level C++ SDK, complex setup
- **Google Meet**: No official SDK—requires running full Chrome instances
- **Microsoft Teams**: Complex authentication and API limitations

**Attendee solves this** by providing a unified REST API that works across all platforms. Instead of spending months building infrastructure, you can:

- ✅ Join meetings in **3 API calls**
- ✅ Get real-time transcripts
- ✅ Record audio/video automatically
- ✅ Build voice agents that speak in meetings
- ✅ Integrate with calendars for scheduled meetings

**Save months of development time** and focus on building your product, not meeting bot infrastructure.

## 💡 Use Cases

Attendee powers a variety of applications:

- **📊 Meeting Analytics** - Build tools like Gong or Chorus.ai
- **📝 Automated Note-Taking** - Create Otter.ai alternatives
- **🤖 Voice Agents** - Deploy AI assistants that join and participate in meetings
- **📹 Meeting Recording** - Automatically record and archive team meetings
- **🔍 Compliance & Quality** - Monitor customer calls for training and compliance
- **📅 Calendar Integration** - Auto-join scheduled meetings from your calendar

## ✨ Key Features

- ✅ **Multi-platform support**: Zoom, Google Meet, Microsoft Teams
- ✅ **Real-time transcription**: Get transcripts as meetings happen
- ✅ **Audio & video recording**: Capture meeting media automatically
- ✅ **Webhooks**: Get notified of bot state changes in real-time
- ✅ **Scheduled meetings**: Automatically join meetings from calendar integrations
- ✅ **Voice agents**: Stream audio/video from websites into meetings
- ✅ **REST API**: Simple, developer-friendly interface
- ✅ **Self-hostable**: Run on your own infrastructure for data privacy
- ✅ **Open source**: Full control, no vendor lock-in

## 🚀 Quick Start

### Option 1: Hosted Instance (Recommended)

1. **Sign up** at [app.attendee.dev](https://app.attendee.dev/accounts/signup/) (free)
2. **Get your API key** from Settings → API Keys
3. **Add credentials** in Settings → Credentials:
   - Zoom OAuth credentials ([how to get them](#-obtaining-zoom-oauth-credentials))
   - Deepgram API key ([sign up free](https://console.deepgram.com/signup))
4. **Start building!** See [API Examples](#-api-examples) below

### Option 2: Self-Host

See [Self Hosting](#self-hosting) section for Docker setup instructions.

## 💻 API Examples

### Join a Meeting

```bash
curl -X POST https://app.attendee.dev/api/v1/bots \
  -H 'Authorization: Token <YOUR_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "meeting_url": "https://us05web.zoom.us/j/84315220467?pwd=...",
    "bot_name": "My Bot"
  }'
```

**Response:**

```json
{
  "id": "bot_3hfP0PXEsNinIZmh",
  "meeting_url": "https://us05web.zoom.us/j/...",
  "state": "joining",
  "transcription_state": "not_started"
}
```

### Get Transcript

```bash
curl -X GET https://app.attendee.dev/api/v1/bots/bot_3hfP0PXEsNinIZmh/transcript \
  -H 'Authorization: Token <YOUR_API_KEY>'
```

**Response:**

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

> 💡 **Tip**: Query the transcript endpoint during the meeting to get real-time partial transcripts!

See the [full API documentation](https://docs.attendee.dev/) for more examples.

## 🏠 Self Hosting

Attendee is designed for easy self-hosting. It runs as a Django app in a single Docker image.

**Requirements:**

- Docker and Docker Compose
- PostgreSQL
- Redis
- AWS S3 (for storage)

**Quick Setup:**

```bash
# 1. Build the image
docker compose -f dev.docker-compose.yaml build

# 2. Create environment file
docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py > .env

# 3. Edit .env with your AWS credentials

# 4. Start services
docker compose -f dev.docker-compose.yaml up

# 5. Run migrations
docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate
```

For detailed setup instructions, see [CONTRIBUTING.md](CONTRIBUTING.md).

## 🤝 Contributing

We welcome contributions! Here's how you can help:

1. ⭐ **Star the repo** - Help others discover Attendee
2. 🐛 **Report bugs** - Open an issue
3. 💡 **Suggest features** - Share your ideas in Slack or GitHub
4. 🔧 **Submit PRs** - See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines
5. 📢 **Spread the word** - Share with your network

**Join our [Slack Community](https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g)** to connect with other developers and get help.

## 📍 Roadmap

- [x] Join and leave Zoom meetings
- [x] Real-time transcripts
- [x] Audio & video recording
- [x] Google Meet support
- [x] Microsoft Teams support
- [x] Webhooks for state changes
- [x] Scheduled meetings via calendar
- [x] Voice agents (stream audio/video into meetings)
- [ ] Webex Support

**Have suggestions?** Join [Slack](https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g) or [open an issue](https://github.com/attendee-labs/attendee/issues).

---

## 📚 Additional Resources

- **[Full Documentation](https://docs.attendee.dev/)** - Complete API reference
- **[Obtaining Zoom OAuth Credentials](#-obtaining-zoom-oauth-credentials)** - Setup guide
- **[Running in Development Mode](#-running-in-development-mode)** - Local development setup

## 🔑 Obtaining Zoom OAuth Credentials

1. Navigate to [Zoom Marketplace](https://marketplace.zoom.us/) and log into your developer account
2. Click "Develop" → "Build App" → Choose "General App"
3. Copy the Client ID and Client Secret from 'App Credentials'
4. Go to Embed tab → Features → Enable Meeting SDK toggle

For detailed instructions, see [this guide](https://developers.zoom.us/docs/meeting-sdk/developer-accounts/) or [watch this video](https://www.loom.com/embed/7cbd3eab1bc4438fb1badcb3787996d6?sid=825a92b5-51ca-447c-86c1-c45f5294ec9d).

## 🧪 Running in Development Mode

See [CONTRIBUTING.md](CONTRIBUTING.md) for complete development setup instructions.

---

<div align="center">

**⭐ If you find Attendee useful, please star the repo!**

Made with ❤️ by the Attendee team

[Website](https://attendee.dev/) · [Documentation](https://docs.attendee.dev/) · [Slack](https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g) · [GitHub](https://github.com/attendee-labs/attendee)

</div>
