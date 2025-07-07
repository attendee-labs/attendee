#!/bin/bash
# this file should go into /usr/local/bin/start-attendee.sh (for Ubuntu)
set -e

# Wait for Docker to be ready
echo "Waiting for Docker to be ready..."
while ! docker system info >/dev/null 2>&1; do
    echo "Docker not ready, waiting..."
    sleep 5
done

echo "Docker is ready, starting services..."
cd /home/ubuntu/attendee
docker compose -f dev.docker-compose.yaml up -d
echo "Services started successfully"