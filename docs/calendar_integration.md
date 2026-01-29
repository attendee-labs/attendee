# Calendar Integration

Attendee's calendar integration lets you automatically schedule bots to join meetings from your users' Google or Microsoft calendars. You store your users' calendar credentials in Attendee, which then syncs events through the Google and Microsoft calendar APIs. Your application's logic decides which events the bots should join.

This feature reduces the time it takes to build a calendar integration for your application, by hiding the complexity of the Google and Microsoft APIs and handling event reschedules and cancellations.

The guide below walks through how to set up the calendar integration in your app. For a reference implementation, see the [Attendee Calendar Integration Example](https://github.com/attendee-labs/calendar-integration-example).

## Create a new Google Calendar OAuth Application

You'll need to create a Google OAuth Application that users integrate with so that Attendee can access their calendar events. You can skip this step if your application won't support Google Calendar. We recommend creating separate apps for development and production.

1. Follow the directions [here](https://support.google.com/googleapi/answer/6158849?hl=en) to create a new Google Cloud project that uses OAuth.
2. Enable the Google Calendar API.
3. Use the scopes `https://www.googleapis.com/auth/calendar.readonly` and `https://www.googleapis.com/auth/userinfo.email` when creating the OAuth client.
4. Google will need to approve your application before external users can authorize it. See [here](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification) for more information. Until your app is approved, only users that are on an allow-list can authorize it. To edit the allow-list navigate to APIs & Services -> OAuth Consent Screen -> Audience and go to the 'Test Users' section.

## Create a new Microsoft Calendar OAuth Application

You'll need to create a Microsoft Calendar OAuth Application that users integrate with so that Attendee can access their calendar events. You can skip this step if your application won't support Microsoft Calendar. We recommend creating separate apps for development and production.

1. Follow the directions [here](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app) to create a new Microsoft Azure Active Directory application. When it asks you to choose 'Supported account types' select 'Accounts in any organizational directory (Any Microsoft Entra ID tenant - Multitenant) and personal Microsoft accounts (e.g. Skype, Xbox)'.
2. For the API permissions, add these scopes: `Calendars.Read`, `User.Read`.
3. Microsoft will need to verify your application before external users can authorize it. See [here](https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview) for more information. This process is entirely automated and should take less than an hour. The steps to get verified are:
    - [Join the Microsoft AI Cloud Partner Program](https://partner.microsoft.com/en-us/partnership).
    - [Configure your app's publisher domain](https://learn.microsoft.com/en-us/entra/identity-platform/howto-configure-publisher-domain).
    - [Mark your app as publisher verified](https://learn.microsoft.com/en-us/entra/identity-platform/mark-app-as-publisher-verified).

## Add calendar webhooks to your Attendee project

If you haven't done so already, we recommend using separate Attendee projects for development and production. These projects will correspond to your development and production OAuth applications.

1. Open your Attendee project and go to Settings -> Webhooks.
2. Click on 'Create Webhook' and select the `calendar.events_update` and `calendar.state_change` triggers. The first will be triggered when new calendar events have been updated or created. The second will be triggered if the calendar becomes disconnected.

## Add OAuth flow logic to your application

You'll need to add code to handle the OAuth flow for users to authorize your Calendar OAuth Applications. The flow is essentially the same for both Google and Microsoft. Follow these steps for each application:

1. Add an `auth` endpoint that your application will use to redirect users to the OAuth flow.
2. Add a `callback` endpoint that your application will use to handle the OAuth callback.
3. In your callback endpoint, you'll exchange the access code for a refresh token.
4. In your callback endpoint, after you've retrieved the refresh token, make a [POST /calendars](https://docs.attendee.dev/api-reference#tag/calendars/post/api/v1/calendars) request to the Attendee API to create a new calendar for the user who just authorized your application. In the request, you'll pass the client id and secret of your application as well as the refresh token. We recommend you pass a deduplication key to prevent duplicate calendars from being created. This could be be the user's email address or internal id.
5. After you make the API request to Attendee, you'll receive a [calendar object](https://docs.attendee.dev/api-reference#model/calendar) in the response. Save this calendar object to your database.

See the `completeOAuthLogin` function in the [example app](https://github.com/attendee-labs/calendar-integration-example/blob/main/server.js) for an example implementation of these steps.

## Add Webhook processing logic to your application for the calendar.events_update trigger

When you receive a webhook with trigger type `calendar.events_update`, it means that events for this calendar have been created or updated. The webhook payload itself does not contain the calendar events. Instead after you receive the webhook, you'll need to make a GET request to the Attendee API to retrieve the newly created or updated calendar events. Here are the steps for doing that:

1. Ensure that your database has a column for each calendar to keep track of the last time you've synced it with Attendee.
2. In your GET request to the [/calendar_events](https://docs.attendee.dev/api-reference#tag/calendars/get/api/v1/calendar_events) endpoint, pass the `calendar_id` parameter to filter for events belonging to that calendar. Also pass the `updated_after` parameter to filter for events that have been updated since the last time you synced the calendar with Attendee.
3. Paginate through the results and save each event to your database.

See the `handleCalendarEventsUpdate` function in the [example app](https://github.com/attendee-labs/calendar-integration-example/blob/main/server.js) for an example implementation.

## Add code to schedule bots for the calendar events to your application

Now that you're syncing the calendar events from Attendee, you'll need to add code to schedule bots for them. To schedule a bot for a calendar event, just use the normal [POST /bots](https://docs.attendee.dev/api-reference#tag/bots/post/api/v1/bots) endpoint, but include the `calendar_event_id` parameter to associate the bot with the calendar event. The resulting bot will be a scheduled bot with a `join_at` time, but unlike normal scheduled bots, the `join_at` will be a read-only property determined by the event's start time. Also, if the calendar event is cancelled, the bot will be deleted.

If you want to unschedule a bot for a calendar event, make a request to the [DELETE /bots/{bot_id}](https://docs.attendee.dev/api-reference#tag/bots/delete/api/v1/bots/{object_id}) endpoint.

The question of which calendar events get bots scheduled for them will depend on your application's business logic. It is possible to schedule multiple bots for a single event. To add an additional safeguard to prevent scheduling duplicate bots for the same event, you can pass the `deduplication_key` parameter in the bot creation request. You can set this value equal to the event's id.

We recommend you add a column to your database to keep track of which calendar events have bots scheduled for them.

## Add Webhook processing logic to your application for the calendar.state_change trigger

When you receive a webhook with trigger type `calendar.state_change`, it means that the calendar has moved to the `disconnected` state. This can happen if the user revokes access to the calendar or their calendar's account is deleted.

On Attendee's side, if a calendar is disconnected, all the scheduled bots associated with the calendar's events will be deleted.

In your application, you should update the calendar in your database to reflect the disconnected state.

See the `handleCalendarStateChange` function in the [example app](https://github.com/attendee-labs/calendar-integration-example/blob/main/server.js) for an example implementation.

## FAQ

### How often does Attendee sync calendar events?

Attendee uses webhooks to sync calendar events from Google and Microsoft nearly instantly. We apply debouncing to webhook updates to reduce server load, so changes can take up to a minute to sync to Attendee. 

### Which events does Attendee sync?

Attendee syncs all events in the calendar that are between 1 day in the past and 28 days in the future. If this window is too small for your application, please reach out.

### Which of a user's calendars does Attendee sync?

This is determined by the `platform_uuid` parameter you pass when creating the calendar via the [POST /calendars](https://docs.attendee.dev/api-reference#tag/calendars/post/api/v1/calendars) request. By default, this parameter is null, which means we sync the user's primary calendar. You can set the parameter to a non-null value to sync a non-primary calendar.