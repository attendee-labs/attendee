# Webex Bots

Attendee bots can join Webex meetings. A Webex bot joins as a guest of your Webex organization, using Cisco's official Webex Browser SDK, and works like the other bots: it can record, transcribe, report participants and speech, and speak or show an image in the meeting.

## Setup

Every project that launches Webex bots needs a Webex **Service App**. Attendee uses it to create the guest identity each bot joins with.

You need a Webex organization on a **paid plan**. Webex only lets paid organizations create guests.

### 1. Create a Service App

1. Sign in to the [Webex Developer Portal](https://developer.webex.com) and go to **My Webex Apps**, then **Create a New App**, then **Service App**.
2. Give it a name and description, and select the `guest-issuer:read` and `guest-issuer:write` scopes.
3. Once it is created, note its **Client ID**, **Client Secret** and **Service App ID**, and your Webex **Organization ID**.

### 2. Have an admin authorize it

A **full admin** of your Webex organization has to authorize the Service App in [Control Hub](https://admin.webex.com): **Apps**, then **Service Apps**, then select the app, **Authorize** and **Save**.

### 3. Add it to Attendee

1. In Attendee, go to **Settings → Credentials** and find the **Webex Service App** card.
2. Click **Add Service App** and enter the Client ID, Client Secret, Service App ID and Organization ID.
3. Click **Authorize with Webex**, and sign in to Webex as the user who created the Service App.

The card then shows the Service App as **Authorized**, and you can launch Webex bots. Attendee keeps the Service App's tokens fresh by itself.

If Webex bots ever fail to connect because the authorization expired or was revoked, click **Authorize with Webex** again.

### Launching a bot

Create the bot with the meeting's URL, as for the other platforms. Two kinds of Webex URL are supported:

| URL | Example |
|---|---|
| Personal room | `https://acme.my.webex.com/meet/jane.doe` |
| Meeting invitation link | `https://acme.my.webex.com/acme.my/j.php?MTID=m0123456789abcdef0123456789abcdef` |

Invitation links carry the meeting password, so you don't need to pass it.

## Additional setup for self-hosting

If you run your own Attendee instance, it needs a Webex **Integration** to authorize your projects' Service Apps. You set this up once per Attendee instance.

1. In the [Webex Developer Portal](https://developer.webex.com), go to **My Webex Apps**, then **Create a New App**, then **Integration**.
2. Set the redirect URI to `https://<your Attendee domain>/projects/webex-service-app/oauth-callback/` (use `http://` for `localhost`).
3. Select only the `spark:applications_token` scope.
4. Set these environment variables to the Integration's credentials:

| Variable | Value |
|---|---|
| `WEBEX_INTEGRATION_CLIENT_ID` | The Integration's Client ID |
| `WEBEX_INTEGRATION_CLIENT_SECRET` | The Integration's Client Secret |

Without them, a Service App can be added but not authorized, and the credentials page says which variables to set.

Attendee refreshes each Service App's tokens weekly from the scheduler (`python manage.py run_scheduler`), so the scheduler needs to be running. A Webex refresh token expires if it goes unused for 90 days.

## Limitations compared to the other bots

- **No chat.** Webex doesn't give guests access to the meeting chat, so the bot can't read or send chat messages.
- **Closed caption transcription is English only.** Only the meeting host can set the language of Webex captions, so there is no caption language setting for Webex. Captions also have to be available on your Webex site (Webex Assistant).
- **Only personal room and invitation links.** Joining by meeting number, and meetings on Webex's government or regional clouds, are not supported.
- **No per participant video streaming.** Recording video works, but `websocket_settings.per_participant_video` is not supported for Webex.
- **Per participant audio for up to 8 recent speakers.** Webex sends the audio of the 8 most recent speakers separately, so per participant audio and transcription stay accurate when people talk over each other. In meetings where more than 8 people take turns, someone who starts talking after a while may have the first second or so of their speech attributed to someone else.
- **Speech start and stop events arrive about a second late,** because that is how quickly Webex reports who is speaking.
- **Screen share audio is not captured.** Screen share start and stop events are reported.
- **The bot can't tell a meeting the host hasn't started from a lobby.** If the bot is waiting in the lobby and nobody else is in the meeting, it treats the meeting as not started and uses the wait for host timeout.
- **Not tested:** webinars and Webex Events, and breakout rooms.
