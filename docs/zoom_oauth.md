# Attendee-managed Zoom OAuth

Attendee's managed Zoom OAuth feature gives your Zoom Bots additional capabilities by generating certain Zoom SDK tokens when they join meetings. Currently, two token types are supported:
- *Local Recording Token*: Lets the bot record meetings without asking permission from the host
- *Onbehalf Token*: Associates the bot with the user it is joining the meeting on behalf of. After February 23, 2026, all bots joining *external* meetings will be required to use this token. See [here](https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/) for the official announcement from Zoom.

Attendee will store your users' Zoom OAuth credentials and use them to generate these tokens. If you'd prefer to manage the credentials yourself and pass the raw tokens to Attendee instead, use the `callback_settings.zoom_tokens_url` parameter when calling the `POST /api/v1/bots` [endpoint](https://docs.attendee.dev/api-reference#tag/bots/post/api/v1/bots).

The guide below walks through how to set up Attendee-managed Zoom OAuth in your app. For a reference implementation, see the [Attendee Managed Zoom OAuth Example](https://github.com/attendee-labs/managed-zoom-oauth-example).

## How it works

When a user authorizes your Zoom app through OAuth:
1. Your application sends the OAuth authorization code to Attendee
2. Attendee exchanges it for OAuth credentials and stores them in Attendee
3. When your bot joins a meeting hosted by that user, Attendee generates a local recording token using the stored credentials.
4. When your app launches a bot on behalf of a user, you pass that user's zoom user id to Attendee in the bot creation request. Attendee will then generate an onbehalf token using the stored credentials.

## Create a Zoom App

You'll need to create a Zoom OAuth App that your users will authorize. We recommend creating separate apps for development and production. You will need to choose whether you want to use the local recording token or the onbehalf token or both. Since the onbehalf token will be required after February 23, 2026, we highly recommend you use it. The local recording token is only needed if your bots are recording meetings and you want to record meetings without asking permission from the host.

1. Go to the [Zoom Developer Portal](https://marketplace.zoom.us/user/build) and create a new General app.
2. On the sidebar select 'Basic Information'.
3. For the OAuth redirect URL, enter your application's OAuth callback URL.
4. On the sidebar select 'Features -> Embed'.
5. Toggle 'Meeting SDK' to on.
6. On the sidebar select 'Scopes'.
7. Add the following scopes if you want to use the local recording token:
   - `user:read:user`
   - `meeting:read:list_meetings`
   - `meeting:read:local_recording_token`
   - `user:read:zak`
8. Add the following scopes if you want to use the onbehalf token:
   - `user:read:user`
   - `user:read:token`

## Register your Zoom App with Attendee

Once you've created your Zoom app, you need to register it with Attendee. We recommend creating Attendee projects for development and production. These projects will correspond to your development and production Zoom applications.

1. Go to **Settings → Credentials**
2. Under Zoom OAuth App Credentials, click **"Add OAuth App"**
3. Enter your Zoom Client ID, Client Secret and Webhook Secret *(Note: Webhook secret is only needed if you are using the local recording token)*

## Configure Zoom App Webhooks

*Note: These steps are only needed if you are using the local recording token.*

If you are using the local recording token, Attendee will keep track of the meetings that are hosted by users who have authorized your app. This is necessary so that Attendee can map a meeting URL to the OAuth credentials that belong to the meeting's host. The host's credentials are used to generate the local recording token for the meeting. In order to keep track of your users' meetings, Attendee needs to be notified when meetings are created.

1. In the Attendee dashboard, click the **Webhook url** button on your newly created Zoom OAuth App credentials.
2. Go back to the Zoom Developer Portal and go to **Features -> Access** in the sidebar.
3. Toggle **Event subscription** and click **Add new Event Subscription**.
4. For the **Event notification endpoint URL**, enter the webhook url you copied earlier from the Attendee dashboard.
5. Select these event types:
   - `Meeting has been created`
   - `User's profile info has been updated`
6. Click **"Save"**
7. If you are creating a production app, validate the webhook by clicking the **Validate** button.

## Configure Attendee webhooks

1. Go to **Settings -> Webhooks**.
2. Click on 'Create Webhook' and select the `zoom_oauth_connection.state_change` trigger. This will be triggered when one your users' Zoom credentials becomes invalid, usually because they uninstalled your app.
3. Click **"Create"** to save your webhook.

## Add OAuth Flow Logic to Your Application

You will need to add code to your application that handles the OAuth flow and calls the Attendee API to create a Zoom OAuth connection for your user.

Follow these steps:

1. Add an `auth` endpoint that your application will use to redirect users to the OAuth flow.
2. Add a `callback` endpoint that your application will use to handle the OAuth callback.
3. In your callback endpoint, you'll take the access code and make a [POST /zoom_oauth_connections](https://docs.attendee.dev/api-reference#tag/zoom-oauth-connections/post/api/v1/zoom_oauth_connections) request to the Attendee API to create a new Zoom OAuth connection for the user who just authorized your application.
5. After you make the API request to Attendee, you'll receive a [Zoom OAuth connection object](https://docs.attendee.dev/api-reference#model/zoom-oauth-connection) in the response. Save this object to your database and associate it with the user who just authorized your application.

See the `/zoom_oauth_callback` route in the [example app](https://github.com/attendee-labs/managed-zoom-oauth-example/blob/main/server.js) for an example implementation of these steps.

## Change your code for launching Zoom bots

For Attendee to use the onbehalf token, you need to specify the zoom user the bot is joining on behalf of. You can do this by passing the user's zoom user id in the `zoom_settings.onbehalf_token.zoom_oauth_connection_user_id` parameter when launching the bot. See the `/api/launch-bot` route in the [example app](https://github.com/attendee-labs/managed-zoom-oauth-example/blob/main/server.js) for an example.

## Add Webhook processing logic to your application for the zoom_oauth_connection.state_change trigger

When you receive a webhook with trigger type `zoom_oauth_connection.state_change`, it means that the Zoom OAuth connection has moved to the `disconnected` state. This can happen if the user revokes access to the Zoom app or their Zoom account is deleted.

In your application, you should update the Zoom OAuth connection in your database to reflect the disconnected state.

See the `/attendee-webhook` route in the [example app](https://github.com/attendee-labs/managed-zoom-oauth-example/blob/main/server.js) for an example implementation.

## FAQ

### Will my Zoom app stop working after February 23, 2026, if we don't use the onbehalf token?

Yes, this is Zoom's [official deadline](https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/). However, Attendee is in contact with Zoom and can request extensions for individual apps that are using Attendee. Please reach out on Slack if you need help getting an extension. Note that if your bot only joins meetings within your Zoom account, you don't need to use the onbehalf token.

### Why can't I delete the Zoom OAuth App credentials?

We don't allow you to delete the Zoom OAuth App credentials if there are any Zoom OAuth connections associated with it. You will need to intentionally delete all the associated Zoom OAuth connections first. You can do this by [listing](https://docs.attendee.dev/api-reference#tag/zoom-oauth-connections/get/api/v1/zoom_oauth_connections) all the associated Zoom OAuth connections and then [deleting](https://docs.attendee.dev/api-reference#tag/zoom-oauth-connections/delete/api/v1/zoom_oauth_connections/{object_id}) them one by one.

### What happens if the onbehalf token user is not in the meeting when the bot joins?

The bot will not be able to join until this user has entered the meeting. Attendee will keep trying to join until a timeout is reached. The timeout can be configured in the `automatic_leave_settings.authorized_user_not_in_meeting_timeout_seconds` parameter when launching the bot. It defaults to 600 seconds.

For more details on onbehalf token related behavior see [here](https://devforum.zoom.us/t/updates-to-meeting-sdk-authorization-faq).

### Are there any alternatives to implementing the onbehalf token?

Yes, you can switch your application to use [Zoom RTMS](https://developers.zoom.us/docs/rtms/). RTMS is a different method for getting recordings and transcripts from meetings which involves an app running in the Zoom client instead of a bot. Attendee has beta support for RTMS, for more information see the example programs for building a [notetaker](https://github.com/attendee-labs/rtms-notetaker-example) and [sales coach](https://github.com/attendee-labs/rtms-sales-coach-example) with Attendee and RTMS.

