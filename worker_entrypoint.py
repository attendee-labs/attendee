#!/usr/bin/env python
"""
Celery worker entrypoint for Cloud Run Worker Pools.

Runs Celery worker and optionally the scheduler in a single container.
No HTTP health check needed - Worker Pools don't require HTTP endpoints.
"""

import os
import subprocess
import sys
import threading


def run_scheduler():
    """Run the scheduler in a background thread."""
    cmd = ["python", "manage.py", "run_scheduler"]
    print(f"Starting Scheduler: {' '.join(cmd)}")
    process = subprocess.Popen(cmd)
    process.wait()


def main():
    # Start scheduler in background if enabled
    if os.getenv("RUN_SCHEDULER", "false").lower() == "true":
        scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()
        print("Scheduler thread started")

    # Run Celery worker in foreground
    celery_args = sys.argv[1:] if len(sys.argv) > 1 else [
        "-A", "attendee", "worker", "-l", "INFO", "--concurrency", "4"
    ]
    celery_cmd = ["celery"] + celery_args
    print(f"Starting Celery: {' '.join(celery_cmd)}")

    process = subprocess.Popen(celery_cmd)
    sys.exit(process.wait())


if __name__ == "__main__":
    main()
