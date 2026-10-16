# Webex Bots

Webex bots join Webex meetings as a **guest**, using Cisco's official [Webex Browser SDK](https://github.com/webex/webex-js-sdk) (the `webex` npm package) running in Chrome. Unlike the Google Meet and Teams bots, they do not pretend to be an anonymous user of the web client.

This document covers how to use them, how Webex works, how the bot is built, and what it can't do. Everything marked **verified** was tried against a real Webex meeting. Everything marked **not verified** was written but not tried.

## Setting up

You need a Webex organization with a paid plan: Webex only lets paid organizations create guests.

1. In the [Webex developer portal](https://developer.webex.com), create a **Service App** with the `guest-issuer:read` and `guest-issuer:write` scopes, and note its client ID, client secret and Service App ID.
2. Ask a **full admin** of your Webex organization to authorize the Service App in Control Hub (Apps, then Service Apps). Until then, requesting tokens fails with `401 ... is not authorized for the targetOrg`.
3. Issue the Service App's tokens. This needs a bearer token with the `spark:applications_token` scope, such as your personal access token from the developer portal:

   ```
   curl -X POST https://webexapis.com/v1/applications/<service app id>/token \
     -H "Authorization: Bearer <personal access token>" \
     -H "Content-Type: application/json" \
     -d '{"clientId": "<client id>", "clientSecret": "<client secret>", "targetOrgId": "<your org id>"}'
   ```

   Keep the `refresh_token` from the response.
4. In Attendee, open the project's credentials page and add the **Webex Service App Credentials**: client ID, client secret and refresh token.

**The refresh token expires after about 90 days** (measured: 7,763,462 seconds). It does not rotate and using it does not extend it, so Attendee never has to write a new one back, but you must repeat step 3 and update the credential before it expires. Access tokens last about 14 days. Attendee refreshes one for every bot that starts, then creates a guest token for that bot.

Creating a bot for a Webex meeting without these credentials is rejected with an error that points to the credentials page.

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
| Transcription from the meeting's captions | **Verified** on one Webex site, including the utterances returned by the transcript API. The bot turns the captions on by itself. |
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
| The Service App credentials are missing, expired or revoked | Could not connect to meeting | Unit tested, and the underlying errors were seen live |

## How Webex works

This is what was learned by reading the SDK (its published source maps), its network traffic, and by experiment.

**Identity.** Webex guests are users minted by a Service App. The Service App is authorized per organization by an admin. It exchanges a refresh token for an access token, and `POST /v1/guests/token` (with a `subject` and a `displayName`) returns a guest access token, valid for about 18 hours. The SDK is initialized with that token: `Webex.init({credentials: {access_token}})`, then `meetings.register()` and `meetings.create(url)`.

**Joining.** `meeting.join()` puts the guest in the lobby if the meeting has one. Media cannot be negotiated from the lobby: calling `joinWithMedia` there fails with "Confluence url for the device is null". The bot therefore joins, waits for `meeting:self:guestAdmitted`, and then calls `meeting.addMedia()`.

**Media.** The SDK uses multistream: instead of one mixed stream, the server sends a few "active speaker" audio streams. The bot asks for 3 audio slots, and for up to 9 active-speaker video panes plus screen share. The slots never say whose audio they carry: the SDK reports their source as "no source", and the RTP packets carry no contributing source. This is why per participant audio is emulated.

A subtle trap: the SDK computes `receiveAudio = audioEnabled && receiveAudio`. Calling `addMedia({audioEnabled: false, additionalMediaOptions: {receiveAudio: true}})` therefore silently turns receiving off, and every audio stream stays muted. The bot keeps `audioEnabled` and `videoEnabled` true. Sending also has to stay enabled (with no local tracks, so nothing is sent), because the send transceivers cannot be added later and the bot publishes its webcam image and audio on demand.

**Speakers.** `media:activeSpeakerChanged` lists the members speaking, in steps of about one second, and the list briefly empties between words.

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

The websocket messages are the same ones the other web bots use, so `WebBotAdapter` handles them unchanged. The Service App credentials are read from the database on the main thread, the requests to Webex run on the adapter's thread, and no secret is kept in memory.

## Limits, trade-offs and parts that may not be reliable

**Per participant audio is emulated.** Because the audio slots don't identify their speaker, each frame of the mixed audio is attributed to the active speaker, as the Teams bot does. Consequences: when people talk over each other, everything is attributed to whoever started speaking first, and the first moments of someone's speech can be missing or attributed to the previous speaker, because speaker changes arrive about a second late. The mixed audio itself is accurate. A speech stop is only reported after the speaker has been absent for 1.5 seconds.

**Mixed audio is clamped.** Summing the three audio slots can go above full scale, and the adapter's float to 16 bit conversion wraps around when that happens, which would be loud distortion. The page clamps the samples.

**"Meeting not started" is a heuristic.** Webex shows a guest waiting for a host that has not started the meeting exactly like a normal lobby. The bot treats a lobby with nobody else in the meeting as "waiting for the host", and uses the (longer) wait-for-host timeout for it. A lobby where the only other people are also waiting guests would be misread.

**Captions need Webex Assistant.** They were verified on one site. Sites or meetings that don't allow captions will give no transcript, and the bot reports that it could not enable them. The caption language is fixed to English; there is no setting for it yet.

**The Service App needs maintenance.** The refresh token has to be replaced about every 90 days, and an expired one only shows up as bots that cannot connect. A dedicated "Webex authorization failed" bot event would be clearer, but it needs a new event subtype and a migration.

**Deviations from how the other bots work.**
- It drives an SDK, not the meeting's web UI, so there are no selectors, no navigation config for the UI, and nothing to break when Webex changes its page.
- The domain allowlist is a fixed list in the adapter (`webex.com`, `ciscospark.com`, `wbx2.com`, `unpkg.com`, `127.0.0.1`), recorded from a real join. The other platforms read theirs from signed navigation configs, and a contributor cannot sign one. Moving it to a `webex.json` is a follow-up for a maintainer.
- The SDK is loaded from unpkg at a pinned version with a Subresource Integrity hash, because the page holds a guest token. Zoom also loads its SDK from a CDN. To upgrade, change the version and recompute the hash (the command is in the page).
- Like the Zoom adapter it has its own small static server for the page (`webex_static_server.py`), and like the other adapters it defines its own `send_video`, `is_sent_video_still_playing` and Chrome policy. These are duplicates of the other adapters on purpose, to keep this change separate from a cleanup of them.

**Not tried at all:** webinars and Webex Events, breakout rooms, a lobby that is turned off (the bot should skip the lobby phase), screen share content, reconnecting after a network drop, and meetings where the bot is the only participant for a long time.

## Developing and testing

**In Docker.** The whole flow was verified with the repository's Docker setup (`dev.docker-compose.yaml`): create a project, add the Webex credentials, create a bot with `POST /api/v1/bots`, admit it, and read back its state, transcript, participant events and recording. Recordings are uploaded to S3, so for a local run point `AWS_ENDPOINT_URL` at an S3-compatible server such as [moto](https://github.com/getmoto/moto) and create the bucket.

**Without Docker.** `bots/webex_bot_adapter/dev/standalone_harness.py` runs the page in a local Chrome without Docker, Django or Xvfb, prints every message the page sends, and saves the audio it receives. See the docstring for the options. It reads the Service App credentials from the environment or a `.env` file at the repo root.

```
python bots/webex_bot_adapter/dev/standalone_harness.py                        # loads the page and the SDK, no meeting needed
python bots/webex_bot_adapter/dev/standalone_harness.py --meeting-url <url or meeting number>
```

The unit tests are `test_webex_bot_adapter.py`, `test_webex_bot_controller.py`, `test_webex_credentials.py`, `test_web_bot_adapter_video_output.py`, `test_web_bot_adapter_domain_allowlist.py` and the Webex cases in `test_meeting_url_utils.py`. One test checks that the page's websocket client defines every method `WebBotAdapter` calls on it, which is the kind of mistake that stops a bot from ever leaving a meeting.
