# FAQ

## How do I get the Zoom client id and secret?

The Zoom app is provided by you, the developer, not by Attendee. When you input the Zoom client ID and secret, you’re specifying the Zoom app that your bot will use to join meetings. See [here](https://github.com/attendee-labs/attendee?tab=readme-ov-file#obtaining-zoom-oauth-credentials) for instructions on how to obtain the client ID and secret.

## Why can't my Zoom bot join external meetings?

Zoom bots must be approved by Zoom to join external meetings. Unapproved Zoom bots can only join meetings that are hosted by the same Zoom account that owns the bot. See [here](https://developers.zoom.us/blog/prepare-meeting-sdk-app-for-review) for the official announcement from Zoom. Please reach out on Slack if you need help getting your bot approved, the process is straightforward.

## Why is the bot having issues joining a Zoom meeting when running Attendee locally? 

You may need to rebuild the docker image. You can do this in one of two ways: 

1. Use the Docker command: `docker compose -f dev.docker-compose.yaml build`

2. Use the Makefile command: `make build`

## Application emits errors when uploading files when running locally. 

This may happen if the AWS_REGION is not set correctly. It currently defaults to `us-east-1`.  
You can set this in the .env file.

## Bot cannot join Google Meet

One reason this can happen is that the bot is being sent to a Google Meet session that only signed in users can join. We currently do not support this scenario where the bot needs to sign in. 

