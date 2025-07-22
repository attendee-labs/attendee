# Signed In Bots

Signed in bots login to a user account for the meeting platform before joining the meeting. By default, bots are not associated with a specific user account, they're doing the equivalent of opening an incognito window and navigating to the meeting URL.

## Why Use Signed In Bots?

*   **Appearance**: Signed-in bots appear as a normal user rather than an anonymous one, so they have an avatar and don't have the 'Unverified' labels that some meeting platforms add for anonymous users. 
*   **Access**: Some meetings are configured to not allow anonymous users to join at all. In these cases, a signed-in bot is required to join the meeting.

The downsides of signed in bots are that it may take slightly longer to join the meeting and there is some setup work required.

## Signed in Zoom Bots

We currently supported signed-in bots for Zoom. Instead of passing a username and password to the bot, you'll pass a ZAK (Zoom Access Key) token, which allows the bot to start or join a meeting on a user's behalf.

To provide the ZAK token to your bot, you must provide a callback URL in the bot creation request. When the bot needs to join a meeting, Attendee will call this URL to request a fresh ZAK token. This callback approach is required because ZAK tokens have a 5-minute lifespan, making it impossible to pass a token directly when creating scheduled bots (as the token would expire before the bot actually joins). 

To provide the callback URL, include the following in your bot creation request:

```json
"callback_settings": {
        "zoom_tokens_url": "https://your-server.com/zoom-tokens-callback"
}
```

Attendee will make a POST request to the callback URL with this data in the body:

```json
{
  "bot_id": "the bot id",
  "bot_metadata": "any metadata you passed in the bot creation request",
  "callback_type": "zoom_tokens",
  "meeting_url": "the meeting URL"
}
```

Your callback endpoint should respond with a JSON object with the following format:

```json
{
  "zak_token": "your_zak_token_here",
}
```

See [here](https://developers.zoom.us/docs/meeting-sdk/auth/#start-meetings-and-webinars-with-a-zoom-users-zak-token) for instructions on how to get the ZAK token for a user, using the Zoom REST API. For most use cases, it makes sense to create a dedicated Zoom user for the bot, and use that user's ZAK token.

## Signed in Teams Bots

We currently support signed-in bots for Microsoft Teams. Here's how to set it up:

1.  Create a new Microsoft Office365 organization to hold the bot's account. You must disable two-factor authentication (2FA) on this organization so that the bot can log in with only an email and password. To disable 2FA, please disable security defaults, following the instructions [here](https://learn.microsoft.com/en-us/entra/fundamentals/security-defaults#disabling-security-defaults). After 2FA is disabled, create a new account in the organization for the bot. If you encounter any issues with this step, please reach out to us on Slack.
2.  Navigate to the Settings -> Credentials page, scroll down to Teams Bot Login Credentials and add the email and password for the bot's account in the organization you created.
3.  When calling the create bot endpoint, you must pass the following parameter to instruct the bot to use the stored credentials to sign in before joining: `"teams_settings": {"use_login": true}`.

## Sign in Google Meet Bots

Currently, we do not support signed-in bots for Google Meet.
