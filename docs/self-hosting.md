# Self-Hosting Attendee

Attendee is designed for convenient self-hosting, installed via the official Docker image.

This guide will walk you through deploying Attendee on development mode, to preview running it on your own infrastructure.

## Overview

Attendee runs as a Django application in a single Docker image with minimal external dependencies:

- **PostgreSQL** - Database for storing bot data, users, and configurations
- **Redis** - Message broker for Celery tasks and caching
- **AWS S3** (optional) - Storage for meeting recordings and media files. If not configured, files will be stored locally


## Prerequisites

Before you begin, ensure you have **Docker** and **Docker Compose** installed on your system. In addition, refer to the Getting Started page for setting up Zoom Oauth and Deepgram for Zoom meetings, if you are using Zoom.

## Running in Development Mode

<scalar-steps>
<scalar-step id="step-0" title="Clone the Repository">
Clone the Attendee repository and navigate to the project directory:

```bash
git clone https://github.com/attendee-labs/attendee.git
cd attendee
```
</scalar-step>

<scalar-step id="step-1" title="Set Up Environment Variables">
Generate the initial environment configuration:

**Linux/Mac:**
```bash
docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py > .env
```

**Windows:**
```powershell
docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py | Out-File -Encoding utf8 .env
```
</scalar-step>

<scalar-step id="step-2" title="Configure Your Environment">
Edit the initial .env file with AWS configuration, if applicable:

```bash
# AWS Configuration (for S3 storage)
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_DEFAULT_REGION=us-east-1
AWS_RECORDING_STORAGE_BUCKET_NAME=your-attendee-bucket
```
</scalar-step>

<scalar-step id="step-3" title="Build and Start Services">
Build the Docker image (takes about 5 minutes):

```bash
docker compose -f dev.docker-compose.yaml build
```

Start all services:

```bash
docker compose -f dev.docker-compose.yaml up -d
```
</scalar-step>

<scalar-step id="step-4" title="Run Database Migrations">
In a separate terminal, run the database migrations:

```bash
docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate
```
</scalar-step>

<scalar-step id="step-5" title="Create Your First Account">
Go to localhost:8000 in your browser and create an account. On creation, the email confirmation link for the account will be written to the server logs in the terminal where you ran:

```bash
docker compose -f dev.docker-compose.yaml up
```

Look for a line like:
```
http://localhost:8000/accounts/confirm-email/<key>/
```

Paste the link into your browser to confirm your account.
</scalar-step>

You should now be able to log in, input your credentials and obtain an API key. API calls should be directed to http://localhost:8000 instead of https://app.attendee.dev.
</scalar-steps>

### Restarting After Changes
After making code changes, the Docker process will automatically detect them and restart. No manual rebuild is needed. However, if you have made changes to the database schema, you'll have to run the migration command again.

## Next Steps

Once you have Attendee running locally, you can:

- **Test bot functionality**: Create bots and join meetings using the same API calls as the hosted version
- **Explore the codebase**: The repository is open source, so you can customize bot behavior and add features
- **Set up webhooks**: Configure webhooks to receive real-time updates about your bots
- **Integrate with calendars**: Set up calendar integration for automatic meeting detection

## Production Deployment

For production deployments for use in your organization, please [schedule a call](https://calendly.com/noah-attendee/30min) with our team. We'll help you set up a secure, scalable production environment and provide ongoing support through a dedicated Slack channel.

