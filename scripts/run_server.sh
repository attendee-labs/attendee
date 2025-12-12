#!/bin/bash
# Run Django development server only (without Celery workers)
# Usage: ./scripts/run_server.sh

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

echo "Starting Django development server..."
echo "Access at: http://localhost:8000"
echo ""

python manage.py runserver 0.0.0.0:8000
