# Signed In Bots

Signed in bots login to a user account for the meeting platform before joining the meeting. By default, bots are not associated with a specific user account, they're doing the equivalent of opening an incognito window and navigating to the meeting URL.

## Why Use Signed In Bots?

*   **Appearance**: Signed-in bots appear as a normal user rather than an anonymous one, so they have an avatar and don't have the 'Unverified' labels that some meeting platforms add for anonymous users. 
*   **Access**: Some meetings are configured to not allow anonymous users to join at all. In these cases, a signed-in bot is required to join the meeting.

The downsides of signed in bots are that it may take slightly longer to join the meeting and there is some setup work required.

## Signed in Zoom Bots

We support signed-in bots for Zoom. Instead of passing a username and password to the bot, you'll pass a ZAK (Zoom Access Key) token, which allows the bot to start or join a meeting on a user's behalf.

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

We support signed-in bots for Microsoft Teams. Here's how to set it up:

1.  Create a new Microsoft Office365 organization to hold the bot's account. You must disable two-factor authentication (2FA) on this organization so that the bot can log in with only an email and password. To disable 2FA, please disable security defaults, following the instructions [here](https://learn.microsoft.com/en-us/entra/fundamentals/security-defaults#disabling-security-defaults). After 2FA is disabled, create a new account in the organization for the bot. If you encounter any issues with this step, please reach out to us on Slack.
2.  Navigate to the Settings -> Credentials page, scroll down to Teams Bot Login Credentials and add the email and password for the bot's account in the organization you created.
3.  When calling the create bot endpoint, you must pass the following parameter to instruct the bot to use the stored credentials to sign in before joining: `"teams_settings": {"use_login": true}`.

## Signed in Google Meet Bots

We support signed-in bots for Google Meet. You'll need to create a new Google Workspace account for the bot to use. You'll then configure the workspace so that the Attendee server is a SAML SSO Identity Provider (IdP) that the bot can use to sign in. This is more reliable than signing in with a username and password.

Here are the steps to set it up:

#### Create a new Google Workspace account for the bot
1. Create a new Google Workspace account for the bot to use. The workspace will need to be on a paid plan and be associated with a domain you own. This can be a subdomain of your main workspace account's domain.
2. Create a non-admin user in the Google Workspace account that the bot will sign in as. The name of this user should be the name of the bot. You can also set their avatar to the desired avatar for the bot.
3. Login as this new user and go through the 'Welcome to Google Workspace' flow. Otherwise, the bot will not be able to sign in as the user.
4. Navigate to the Admin Console, then to Security -> Set up single sign-on (SSO) with a third party IdP.
5. On the SSO page, click the "Add SAML Profile" button.
6. At the bottom of the SAML Profile page click the "Legacy SSO profile" link.
7. Create a certificate and private key for the SSO profile. You can use the following command to generate a certificate and private key:
    ```
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -sha256 -days 3650 -nodes
    ```
8. In the Legacy SSO profile page, enter the following information:
    - Enable legacy SSO profile - Yes
    - Sign-in URL - https://app.attendee.dev/bot_sso/google_meet_sign_in (For self-hosted instances, use the hostname of your instance instead of app.attendee.dev)
    - Sign-out URL - https://app.attendee.dev/bot_sso/google_meet_sign_out (For self-hosted instances, use the hostname of your instance instead of app.attendee.dev)
    - Use a domain-specific issuer - Yes
    - Certificate - Upload the cert.pem file you generated in the previous step
9. Click the "Save" button.
10. Return to the Security -> Set up single sign-on (SSO) with a third party IdP page.
11. Under the Manage SSO profile assignments section, click the "Manage" button.
12. Select the Legacy SSO profile you created in the previous step and click the "Save" button.

#### Create a new Google Meet bot login for your Attendee project
1. Navigate to the Settings -> Credentials page in the Attendee dashboard.
2. Scroll down to the Google Meet Bot Logins section and click the "Add Login" button.
3. Enter the requested information. For private key and certificate, you can upload the key.pem and cert.pem files you generated previously. The email must match the email of the bot user in your Google Workspace account. The certificate must match the certificate you added when creating the Legacy SSO profile.

#### Configure bots to use the Google Meet bot login
You need to pass the following parameter to the create bot endpoint to activate the Google Meet bot login: `"google_meet_settings": {"use_login": true}`. There is also an optional parameter called `login_mode` that can be set to `"only_if_required"` to only login if the meeting requires authentication. The default is `"always"`, which means the bot will always login.

#### How many Google Meet bot logins should I create?
Google has concurrency limits for the number of meetings a single account can have open at once. To avoid running into these limits, you can create multiple Google Meet bot logins and we will assign the logins to the bots in a round-robin fashion. The recommended number of logins is MAX_NUMBER_OF_CONCURRENT_MEETINGS / 20. For example, if your application peaks at 100 concurrent meetings, you should create 5 Google Meet bot logins.
