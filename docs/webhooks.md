# Webhooks

Webhooks send your server real-time updates when something important happens in Attendee, so that you don't need to poll the API.

They can alert your server when a bot joins a meeting, starts recording, when a recording is available, when a chat message is sent, when the transcript is updated, when participants join or leave meetings, or when calendar events are updated.

Attendee supports two types of webhooks:
- **Project-level webhooks**: Apply to all bots in a project (created via UI)
- **Bot-level webhooks**: Apply to specific bots only (created via API)

## Creating Project-Level Webhooks

To create a project-level webhook via the UI:

1. Click on "Settings → Webhooks" in the sidebar
2. Click "Create Webhook" 
3. Provide an HTTPS URL that will receive webhook events
4. Select the triggers you want to receive notifications for (we currently have six triggers: `bot.state_change`, `transcript.update`, `chat_messages.update`, `participant_events.join_leave`, `calendar.events_update`, and `calendar.state_change`)
5. Click "Create" to save your subscription

## Creating Bot-Level Webhooks

Bot-level webhooks are created via the API when creating a bot. Include a `webhooks` field in your bot creation request:

```json
{
  "meeting_url": "https://zoom.us/j/123456789",
  "bot_name": "My Bot with Webhooks",
  "webhooks": [
    {
      "url": "https://my-app.com/bot-webhook",
      "triggers": ["bot.state_change", "transcript.update"]
    },
    {
      "url": "https://backup-webhook.com/events",
      "triggers": ["bot.state_change", "chat_messages.update"]
    }
  ]
}
```

### Available Webhook Triggers

| Trigger | Description |
|---------|-------------|
| `bot.state_change` | Bot changes state (joins, leaves, starts recording, etc.) |
| `transcript.update` | Real-time transcript updates during meeting |
| `chat_messages.update` | Chat message updates in the meeting |
| `participant_events.join_leave` | A participant joins or leaves the meeting |
| `calendar.events_update` | Calendar events have been synced and updated |
| `calendar.state_change` | Calendar connection state has changed (connected/disconnected) |

## Webhook Delivery Priority

When a bot has both project-level and bot-level webhooks configured, the bot-level webhooks will be used instead of the project-level webhooks.

## Webhook Limits and Validation

- **Maximum**: 2 webhooks per project or per bot
- **URL Format**: Must start with `https://`
- **Uniqueness**: Same URL cannot be used multiple times for the same bot/project

## Webhook Payload

When a webhook is delivered, Attendee will send an HTTP POST request to your webhook URL with the following structure:

### For Bot-Related Events

```
{
  "idempotency_key": < UUID that uniquely identifies this webhook delivery >,
  "bot_id": < Id of the bot associated with the webhook delivery >,
  "bot_metadata": < Any metadata associated with the bot >,
  "trigger": < Trigger for the webhook. Currently, the four bot-related triggers are bot.state_change, which is fired whenever the bot changes its state, transcript.update which is fired when the transcript is updated, chat_messages.update which is fired when a chat message is sent and participant_events.join_leave which is fired when a participant joins or leaves the meeting. >,
  "data": < Trigger-specific data >
}
```

### For Calendar-Related Events

```
{
  "idempotency_key": < UUID that uniquely identifies this webhook delivery >,
  "calendar_id": < Id of the calendar associated with the webhook delivery >,
  "calendar_deduplication_key": < Deduplication key of the calendar (if set) >,
  "calendar_metadata": < Any metadata associated with the calendar >,
  "trigger": < Trigger for the webhook. Currently, the two calendar-related triggers are calendar.events_update which is fired when the calendar events have been updated and calendar.state_change which is fired when the calendar becomes disconnected. >,
  "data": < Trigger-specific data >
}
```

### Payload for `bot.state_change` trigger

For webhooks triggered by `bot.state_change`, the `data` field contains:

```
{
  "new_state": < The current state of the bot >,
  "old_state": < The previous state of the bot >,
  "created_at": < The timestamp when the state change occurred >,
  "event_type": < The type of event that triggered the state change >,
  "event_sub_type": < The sub-type of event that triggered the state change >,
}
```

### Using webhooks to know when the recording is available

The most common use case for webhooks is to be notified when the meeting has ended and the recording is available. You can do this by listening for the `post_processing_completed` event type.

The data field will look like this

```json
{
  "new_state": "ended",
  "old_state": "post_processing",
  "created_at": "2023-07-15T14:30:45.123456Z",
  "event_type": "post_processing_completed",
  "event_sub_type": null,
}
```

### Payload for `transcript.update` trigger

For webhooks triggered by `transcript.update`, the `data` field contains a single utterance:

```
{
  "speaker_name": <The name of the speaker>,
  "speaker_uuid": <The UUID of the speaker within the meeting>,
  "speaker_user_uuid": <The UUID of the speaker's user account within the meeting platform>,
  "timestamp_ms": <The timestamp of the utterance in milliseconds>,
  "duration_ms": <The duration of the utterance in milliseconds>,
  "transcription": {
    "transcript": <The utterance text>,
    "words": <The word-level timestamps of the utterance if they exist>,
  },
}
```

### Payload for `chat_messages.update` trigger

For webhooks triggered by `chat_messages.update`, the `data` field contains a single chat message:

```
{
  "id": <The ID of the chat message>,
  "to": <Whether the message was sent to the bot or to everyone>,
  "text": <The text of the chat message>,
  "timestamp": <The timestamp of the chat message>,
  "sender_name": <The name of the participant who sent the chat message>,
  "sender_uuid": <The UUID of the participant who sent the chat message>,
  "timestamp_ms": <The timestamp of the chat message in milliseconds>,
  "additional_data": <Any additional data associated with the chat message>,
  "sender_user_uuid": <The UUID of the participant's user account within the meeting platform>,
}
```

### Payload for `participant_events.join_leave` trigger

For webhooks triggered by `participant_events.join_leave`, the `data` field contains a single participant event:

```
{
  "id": <The ID of the participant event>,
  "participant_name": <The name of the participant who joined or left the meeting>,
  "participant_uuid": <The UUID of the participant who joined or left the meeting>,
  "participant_user_uuid": <The UUID of the participant's user account within the meeting platform>,
  "event_type": <The type of event that occurred. Either "join" or "leave">,
  "event_data": <Any additional data associated with the event. This is empty for join and leave events>,
  "timestamp_ms": <The timestamp of the event in milliseconds>,
}
```

### Payload for `calendar.events_update` trigger

For webhooks triggered by `calendar.events_update`, the `data` field contains calendar sync information:

```
{
  "state": <The current state of the calendar connection ("connected" or "disconnected")>,
  "connection_failure_data": <Any error data if the calendar is disconnected, null if connected>,
  "last_successful_sync_at": <The timestamp of the last successful calendar sync>,
  "last_attempted_sync_at": <The timestamp of the last sync attempt>,
}
```

This webhook is triggered after each successful calendar sync operation, which occurs automatically to keep your calendar events up to date with the remote calendar (Google Calendar or Microsoft Calendar). After receiving this webhook, you can fetch the calendar events from the Attendee API to get the latest events.

### Payload for `calendar.state_change` trigger

For webhooks triggered by `calendar.state_change`, the `data` field contains the same structure as `calendar.events_update`:

```
{
  "state": <The current state of the calendar connection ("connected" or "disconnected")>,
  "connection_failure_data": <Error details when the calendar becomes disconnected>,
  "last_successful_sync_at": <The timestamp of the last successful calendar sync>,
  "last_attempted_sync_at": <The timestamp of the last sync attempt>,
}
```

This webhook is triggered when a calendar's connection state changes, typically when authentication fails and the calendar becomes disconnected. The `connection_failure_data` field will contain error details such as:

```json
{
  "error": "Google Authentication error: {'error': 'invalid_grant'}",
  "timestamp": "2023-07-15T14:30:45.123456Z"
}
```

## Debugging Webhook Deliveries

Go to the 'Bots' page and navigate to a Bot which was created after you created your webhook. You should see a 'Webhooks' tab on the page. Clicking it will show a list of all the webhook deliveries for that bot, whether they succeeded and the response from your server.

## Verifying Webhooks

To ensure the webhook requests are coming from Attendee, we sign each request with a secret. You can verify this signature to confirm the authenticity of the request.

- Each project has a single webhook secret used for both project and bot-level webhooks. You can get the secret in the Settings → Webhooks page.
- The signature is included in the `X-Webhook-Signature` header of each webhook request

## Webhook Retry Policy

If your endpoint returns a non-2xx status code or fails to respond within 10 seconds, Attendee will retry the webhook delivery up to 3 times with exponential backoff.

## Code examples for processing webhooks

Here are some code examples for processing webhooks in different languages.

### Python

This is a simple flask server that runs on port 5005. It listens for webhook requests and verifies the signature.
```python
import json
import logging
import hmac
import hashlib
import base64

from flask import Flask, request

app = Flask(__name__)
port = 5005

# Add your secret you got from the dashboard here
webhook_secret = "<YOUR_SECRET>"

def sign_payload(payload, secret):
    """
    Sign a webhook payload using HMAC-SHA256. Returns a base64-encoded HMAC-SHA256 signature
    """
    # Convert the payload to a canonical JSON string
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    # Decode the secret
    secret_decoded = base64.b64decode(secret)

    # Create the signature
    signature = hmac.new(secret_decoded, payload_json.encode("utf-8"), hashlib.sha256).digest()

    # Return base64 encoded signature
    return base64.b64encode(signature).decode("utf-8")

@app.route("/", methods=["POST"])
def webhook():
    # Try to parse as JSON
    payload = json.loads(request.data)
    print("Received payload =", payload)
    signature_from_header = request.headers.get("X-Webhook-Signature")
    signature_from_payload = sign_payload(payload, webhook_secret)
    print("signature_from_header =", signature_from_header)
    print("signature_from_payload =", signature_from_payload)
    if signature_from_header != signature_from_payload:
        return "Invalid signature", 400
    print("Signature is valid")

    # Respond with 200 OK
    return "Webhook received successfully", 200


if __name__ == "__main__":
    print(f"Webhook server running at http://localhost:{port}")
    print("Ready to receive webhook requests")
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)  # Only show errors, not request info
    app.run(host="0.0.0.0", port=port, debug=False)
```

### Javascript (Node.js)

This is a simple express server that runs on port 5005. It listens for webhook requests and verifies the signature.

```javascript
// webhook.js
import express from "express";
import crypto from "crypto";

const app  = express();
const port = 5005;

// Put the base‑64 secret from your dashboard here
const WEBHOOK_SECRET = "<YOUR_SECRET>";

/* ---- helpers ----------------------------------------------------------- */

function sortKeys(value) {
  if (Array.isArray(value))           return value.map(sortKeys);
  if (value && typeof value === "object" && !(value instanceof Date)) {
    return Object.keys(value)
      .sort()
      .reduce((acc, k) => ({ ...acc, [k]: sortKeys(value[k]) }), {});
  }
  return value;
}

/** Sign a payload and return a base‑64 HMAC‑SHA256 digest */
function signPayload(payload, secretB64) {
  const canonical = JSON.stringify(sortKeys(payload));
  const secretBuf = Buffer.from(secretB64, "base64");
  return crypto
    .createHmac("sha256", secretBuf)
    .update(canonical, "utf8")
    .digest("base64");
}

/* ---- middleware & route ----------------------------------------------- */

app.use(express.json({ limit: "1mb" })); // parse JSON body

app.post("/", (req, res) => {
  const payload              = req.body;
  const signatureFromHeader  = req.header("X-Webhook-Signature") || "";
  const signatureCalculated  = signPayload(payload, WEBHOOK_SECRET);

  console.log("Received payload =", payload);
  console.log("signature_from_header =", signatureFromHeader);
  console.log("signature_from_payload =", signatureCalculated);

  if (signatureCalculated !== signatureFromHeader) {
    console.log("Signature is invalid")
    return res.status(400).send("Invalid signature");
  }

  console.log("Signature is valid");
  res.send("Webhook received successfully");
});

/* ---- start server ------------------------------------------------------ */

app.listen(port, () => {
  console.log(`Webhook server running at http://localhost:${port}`);
  console.log("Ready to receive webhook requests");
});
```
