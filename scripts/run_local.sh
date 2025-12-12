#!/bin/bash
# Run all Attendee services locally (without Docker)
# Usage: ./scripts/run_local.sh

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

echo "==================================="
echo "Starting Attendee Services (Local)"
echo "==================================="
echo ""

# Check if PostgreSQL is running
if ! pg_isready -h localhost -p 5432 > /dev/null 2>&1; then
    echo "Warning: PostgreSQL doesn't appear to be running."
    echo "Start it with: sudo service postgresql start"
    echo ""
fi

# Check if Redis is running
if ! redis-cli ping > /dev/null 2>&1; then
    echo "Warning: Redis doesn't appear to be running."
    echo "Start it with: sudo service redis-server start"
    echo ""
fi

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput

echo ""
echo "Starting services in background..."
echo ""

# Create a logs directory
mkdir -p logs

# Start Celery worker in background
echo "Starting Celery worker..."
celery -A attendee worker -l INFO > logs/celery_worker.log 2>&1 &
WORKER_PID=$!
echo "Celery worker started (PID: $WORKER_PID)"

# Start Celery scheduler in background
echo "Starting Celery scheduler..."
python manage.py run_scheduler > logs/celery_scheduler.log 2>&1 &
SCHEDULER_PID=$!
echo "Celery scheduler started (PID: $SCHEDULER_PID)"

# Save PIDs for later cleanup
echo "$WORKER_PID" > logs/worker.pid
echo "$SCHEDULER_PID" > logs/scheduler.pid

echo ""
echo "==================================="
echo "Starting Django development server"
echo "==================================="
echo ""
echo "Access the application at: http://localhost:8000"
echo "API documentation at: http://localhost:8000/api/v1/"
echo ""
echo "Press Ctrl+C to stop all services"
echo ""

# Function to cleanup background processes
cleanup() {
    echo ""
    echo "Stopping services..."

    if [ -f logs/worker.pid ]; then
        kill $(cat logs/worker.pid) 2>/dev/null || true
        rm logs/worker.pid
    fi

    if [ -f logs/scheduler.pid ]; then
        kill $(cat logs/scheduler.pid) 2>/dev/null || true
        rm logs/scheduler.pid
    fi

    echo "All services stopped."
    exit 0
}

# Set up trap to cleanup on exit
trap cleanup SIGINT SIGTERM

# Start Django development server in foreground
python manage.py runserver 0.0.0.0:8000
