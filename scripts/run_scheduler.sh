#!/bin/bash
# Run Celery scheduler
# Usage: ./scripts/run_scheduler.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Set Django settings module for local development
export DJANGO_SETTINGS_MODULE=attendee.settings.local

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

echo "Starting Celery scheduler..."
python manage.py run_scheduler
