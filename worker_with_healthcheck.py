#!/usr/bin/env python
"""
Celery worker wrapper with HTTP health check for Cloud Run.
Runs a simple health check server alongside the Celery worker.
"""

import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple health check handler."""

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress health check logs to reduce noise
        pass


def run_health_server(port):
    """Run the health check server."""
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"Health check server running on port {port}")
    server.serve_forever()


def main():
    # Get port from environment (Cloud Run sets PORT)
    port = int(os.environ.get("PORT", 8080))

    # Start health check server in background thread
    health_thread = threading.Thread(target=run_health_server, args=(port,), daemon=True)
    health_thread.start()

    # Build celery command from remaining args or use defaults
    celery_args = sys.argv[1:] if len(sys.argv) > 1 else [
        "-A", "attendee", "worker", "-l", "INFO", "--concurrency", "4"
    ]

    # Run celery worker in foreground
    celery_cmd = ["celery"] + celery_args
    print(f"Starting Celery: {' '.join(celery_cmd)}")

    process = subprocess.Popen(celery_cmd)
    sys.exit(process.wait())


if __name__ == "__main__":
    main()
