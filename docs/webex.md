# Webex Bots

Webex bots join Webex meetings as a **guest**, using Cisco's official [Webex Browser SDK](https://github.com/webex/webex-js-sdk) (the `webex` npm package) running in Chrome. Unlike the Google Meet and Teams bots, they do not pretend to be an anonymous user of the web client.

This document covers how to use them, how Webex works, how the bot is built, and what it can't do. Everything marked **verified** was tried against a real Webex meeting. Everything marked **not verified** was written but not tried.

## Setting up

### Once per Attendee deployment (self-hosting)

Attendee issues each Service App's tokens through its own Webex **Integration**, which the user signs in to once. To set it up:

1. In the [Webex developer portal](https://developer.webex.com), create an **Integration** with only the `spark:applications_token` scope, and the redirect URI `https://<SITE_DOMAIN>/projects/webex-service-app/oauth-callback/` (`http://` for `localhost`).
2. Set the `WEBEX_INTEGRATION_CLIENT_ID` and `WEBEX_INTEGRATION_CLIENT_SECRET` environment variables to the Integration's client ID and client secret.

Without them, a Service App can be added but not authorized, and the credentials page says which variables to set.

### Once per project

You need a Webex organization with a paid plan: Webex only lets paid organizations create guests.

1. In the [Webex developer portal](https://developer.webex.com), create a **Service App** with the `guest-issuer:read` and `guest-issuer:write` scopes. Note its client ID, client secret and Service App ID, and your organization ID.
2. Ask a **full admin** of your Webex organization to authorize the Service App in Control Hub (Apps, then Service Apps, then Authorize and Save).
3. In Attendee, open the project's credentials page, add the **Webex Service App** with those four values, then click **Authorize with Webex** and sign in as the creator of the Service App.

Attendee then issues the Service App's tokens with `POST /v1/applications/{id}/token` and saves its refresh token. **The refresh token lasts 90 days, and using it resets that to 90 days**, so this is done once. Attendee's scheduler refreshes every Service App that has not been refreshed in 7 days, and saves the refresh token Webex returns. Webex only resets the lifetime once the token is a few days old: a token used repeatedly on its first day kept counting down, while the same token at 5 days old went from 84.97 back to 90 days after a single call. If a refresh token does expire or is revoked anyway, click **Authorize with Webex** again. Access tokens last about 14 days. Attendee refreshes one for every bot that starts, then creates a guest token for that bot.

Creating a bot for a Webex meeting without an authorized Service App is rejected with an error that points to the credentials page.

## Meeting URLs

Two kinds of Webex URL are accepted:

| URL | Example |
|---|---|
| Personal room | `https://acme.my.webex.com/meet/jane.doe` |
| Invitation link | `https://acme.my.webex.com/acme.my/j.php?MTID=m0123456789abcdef0123456789abcdef` |

The host must be on `webex.com`. Everything except the room name or the `MTID` is dropped. Other Webex URLs, such as the browser launcher page (`.../wbxmjs/joinservice/...`), are rejected because the SDK can't join them (**verified**: it answers "Invalid invitee address"). URLs on other domains, such as those of Webex's government and regional clouds, are not recognized.

**Passwords.** Invitation links carry the meeting password themselves, so a bot joining through one never needs it. Personal rooms have no password setting. Webex only enforces a password when joining by meeting *number*, which the API can't express today because it takes a URL. The bot implements the wrong-password check for number-based joins (**verified** with the development harness), but nothing can trigger it through the API yet.

## What works

| Feature | Status |
|---|---|
| Join as a guest, wait in the lobby, be admitted | **Verified** |
| Participant join and leave | **Verified** |
| Speech start and stop | **Verified**, also as participant events through the API, delayed by about a second (see below) |
| Transcription from the meeting's captions | **Verified** on one Webex site, including the utterances returned by the transcript API. The bot turns the captions on by itself, only when the bot is transcribed with them. |
| Mixed audio | **Verified** (48 kHz mono float, the same format as the other web bots) |
| Per participant audio | **Verified, but emulated** (see below) |
| Video and audio recording | **Verified** in the Docker image: an 11 minute recording was 1920x1080 H.264 at 30 fps with 48 kHz AAC audio, showing the host's camera with the participant's name and containing real speech |
| Bot webcam image | **Verified** |
| Bot audio output | **Verified** with a test tone |
| Bot video output (`send_video`) | Shared code with the other web bots, **not verified** for Webex |
| Reading chat messages | **Not possible** for a guest (see below) |
| Sending chat messages | **Not possible** for a guest |

### Failure cases

| Situation | What the controller is told | Status |
|---|---|---|
| Meeting does not exist | Meeting not found | Verified |
| Wrong password (joining by number) | Incorrect password | Verified |
| The host denies the bot in the lobby | Request to join denied | Verified |
| The bot is removed from the meeting | Meeting ended, with the removal recorded | Verified |
| The meeting ends | Meeting ended | Verified |
| The host has not started the meeting | Waiting for host timeout | The wait itself is **verified** (a full 10 minute timeout against a meeting nobody was in), see "Not started" below |
| Stuck in the lobby too long | Waiting room timeout | Unit tested only |
| The Service App is missing or not authorized, or its refresh token expired or was revoked | Could not connect to meeting | Unit tested, and the underlying errors were seen live |

## How Webex works

This is what was learned by reading the SDK (its published source maps), its network traffic, and by experiment.

**Identity.** Webex guests are users minted by a Service App. The Service App is authorized per organization by an admin. It exchanges a refresh token for an access token, and `POST /v1/guests/token` (with a `subject` and a `displayName`) returns a guest access token, valid for about 18 hours. The SDK is initialized with that token: `Webex.init({credentials: {access_token}})`, then `meetings.register()` and `meetings.create(url)`.

**Joining.** `meeting.join()` puts the guest in the lobby if the meeting has one. Media cannot be negotiated from the lobby: calling `joinWithMedia` there fails with "Confluence url for the device is null". The bot therefore joins, waits for `meeting:self:guestAdmitted`, and then calls `meeting.addMedia()`.

**Media.** The SDK uses multistream: instead of one mixed stream, the server sends a few "active speaker" audio streams. The bot asks for 3 audio slots, and for up to 9 active-speaker video panes plus screen share. The slots never say whose audio they carry: the SDK reports their source as "no source", and the RTP packets carry no contributing source. This is why per participant audio is emulated.

A subtle trap: the SDK computes `receiveAudio = audioEnabled && receiveAudio`. Calling `addMedia({audioEnabled: false, additionalMediaOptions: {receiveAudio: true}})` therefore silently turns receiving off, and every audio stream stays muted. The bot keeps `audioEnabled` and `videoEnabled` true. Sending also has to stay enabled (with no local tracks, so nothing is sent), because the send transceivers cannot be added later and the bot publishes its webcam image and audio on demand.

**Speakers.** `media:activeSpeakerChanged` lists the members speaking, in steps of about one second, and the list briefly empties between words. Measured against the audio, a speaker is reported 0.1 to 0.7 seconds after their voice starts and 0.2 to 1.4 seconds after it stops. The speaker named has always been the right one. Webex only reports speech: a participant playing a tone or music is never reported as speaking.

**Captions.** `meeting.startTranscription()` enables Webex's built-in captions and `meeting:caption-received` delivers them, including interim versions that are replaced by a final one. `startTranscription()` swallows its own errors and fails with "can not turn on captions before llm connected" when called right after joining, so the bot calls it, waits for the transcription announcement, and calls it again.

**Leaving.** Webex reports why the meeting object was destroyed (`DESTROY_MEETING`): `SELF_REMOVED` (denied in the lobby, or removed afterwards), `MEETING_INACTIVE_TERMINATING` (the meeting ended). When the bot leaves by itself the destroy event has no reason at all, and a removal event follows. Roles are hidden from lobby guests, so a guest cannot tell whether the host is present.

**Chat.** In-meeting chat lives in a "meeting container" that is not part of the meetings API. For a guest token the container's endpoint answers `403 Forbidden`, and no conversation events reach the bot while it is admitted (**verified**, including with the browser's CORS check turned off in a development run).

## How the bot works

```
WebexBotAdapter (Python, bots/webex_bot_adapter/)
  loads webex_chromedriver_page.html in Chrome, from a local static server
  passes the guest token, meeting URL and bot name in window.webexInitialData
  polls window.webexJoinState for the phase: joining, lobby, admitted, joined
  turns failures into the Ui*Exception the join loop already handles

webex_chromedriver_page.js (runs the SDK)
  joins, waits for admission, adds media
  reports participants, speech, captions and failures over the websocket
  plays the remote audio, mixes it, sends mixed and per participant audio
  renders the remote video

webex_chromedriver_payload.js: the websocket client shared by the page
```

The websocket messages are the same ones the other web bots use, so `WebBotAdapter` handles them unchanged. The Service App is read from the database on the main thread, the requests to Webex run on the adapter's thread, and no secret is kept in memory.

## Limits, trade-offs and parts that may not be reliable

**Per participant audio is emulated.** Because the audio slots don't identify their speaker, each frame of the mixed audio is attributed to the active speaker, as the Teams bot does. Like the Teams bot, the page holds the audio back (`perParticipantAudioUtteranceDelayMs`, 2 seconds) until the speaker reports covering it have arrived, and counts a speaker as speaking from 0.7 seconds before they were first reported. In a test with a second participant speaking on a known schedule and quick handovers between them, 85% of that participant's speech and 92% of the other speaker's went to the right person, and nearly all of the rest was in the stretches where both talked at once. The previous approach, without the delay, gave the scheduled participant only 27% of their speech. Consequences: when people talk over each other, everything goes to whoever started speaking last; sound from someone Webex does not report as speaking (music, a tone, background noise) goes to whoever is speaking; and the per participant audio arrives 2 seconds late, which utterance timestamps are corrected for. The mixed audio itself is accurate. A speech stop is only reported after the speaker has been absent for 1.5 seconds.

**Mixed audio is clamped.** Summing the three audio slots can go above full scale, and the adapter's float to 16 bit conversion wraps around when that happens, which would be loud distortion. The page clamps the samples.

**"Meeting not started" is a heuristic.** Webex shows a guest waiting for a host that has not started the meeting exactly like a normal lobby. The bot treats a lobby with nobody else in the meeting as "waiting for the host", and uses the (longer) wait-for-host timeout for it. A lobby where the only other people are also waiting guests would be misread.

**Captions need Webex Assistant.** They were verified on one site. Sites or meetings that don't allow captions will give no transcript, and the bot reports that it could not enable them. The caption language is fixed to English; there is no setting for it yet.

**The refresh token's reset is not documented by Webex.** Attendee relies on the observed behavior that using a refresh token resets its 90 day lifetime. If Webex changes that, the token expires 90 days after it was issued, and an expired one only shows up as bots that cannot connect and an error in the scheduler's logs. A dedicated "Webex authorization failed" bot event would be clearer, but it needs a new event subtype and a migration.

**Deviations from how the other bots work.**
- It drives an SDK, not the meeting's web UI, so there are no selectors, no navigation config for the UI, and nothing to break when Webex changes its page.
- The domain allowlist is a fixed list in the adapter (`webex.com`, `ciscospark.com`, `wbx2.com`, `unpkg.com`, `127.0.0.1`), recorded from a real join. The other platforms read theirs from signed navigation configs, and a contributor cannot sign one. Moving it to a `webex.json` is a follow-up for a maintainer.
- The SDK is loaded from unpkg at a pinned version with a Subresource Integrity hash, because the page holds a guest token. Zoom also loads its SDK from a CDN. To upgrade, change the version and recompute the hash (the command is in the page).
- Like the Zoom adapter it has its own small static server for the page (`webex_static_server.py`), and like the other adapters it defines its own `send_video`, `is_sent_video_still_playing` and Chrome policy. These are duplicates of the other adapters on purpose, to keep this change separate from a cleanup of them.

**Not tried at all:** webinars and Webex Events, breakout rooms, a lobby that is turned off (the bot should skip the lobby phase), screen share content, reconnecting after a network drop, and meetings where the bot is the only participant for a long time.

## Developing and testing

**In Docker.** The whole flow was verified with the repository's Docker setup (`dev.docker-compose.yaml`): create a project, add the Webex Service App, create a bot with `POST /api/v1/bots`, admit it, and read back its state, transcript, participant events and recording. Recordings are uploaded to S3, so for a local run point `AWS_ENDPOINT_URL` at an S3-compatible server such as [moto](https://github.com/getmoto/moto) and create the bucket.

**Without Docker.** `bots/webex_bot_adapter/dev/standalone_harness.py` runs the page in a local Chrome without Docker, Django or Xvfb, prints every message the page sends, and saves the audio it receives. See the docstring for the options. It reads the Service App's client ID, client secret and refresh token from the environment or a `.env` file at the repo root.

```
python bots/webex_bot_adapter/dev/standalone_harness.py                        # loads the page and the SDK, no meeting needed
python bots/webex_bot_adapter/dev/standalone_harness.py --meeting-url <url or meeting number>
```

The unit tests are `test_webex_bot_adapter.py`, `test_webex_bot_controller.py`, `test_webex_service_app.py`, `test_refresh_webex_service_app_task.py`, `test_webex_domain_allowlist.py` and the Webex cases in `test_meeting_url_utils.py`. One test checks that the page's websocket client defines every method `WebBotAdapter` calls on it, which is the kind of mistake that stops a bot from ever leaving a meeting.
