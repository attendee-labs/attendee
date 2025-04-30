# FAQ

## How do I get the Zoom client id and secret?

The Zoom app is provided by you, the developer, not by Attendee. When you input the Zoom client ID and secret, you’re specifying the Zoom app that your bot will use to join meetings.

This means you’ll need to go through Zoom’s app approval process.

## Why can't I join a Zoom meeting when running the app locally? 

For Zoom to work locally, you need to rebuild the docker image. You can do this in one of two ways: 

1. Use the Docker command: `docker compose -f dev.docker-compose.yaml build`

2. Use the Makefile command: `make build`

## Application emits errors when uploading files. 

This may happen if the AWS_REGION is not set correctly. It currently defaults to `us-east-1`.  
You can set this in the .env file.

