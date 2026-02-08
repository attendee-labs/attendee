import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from django.core.management.base import BaseCommand

from bots.tasks import run_bot

logger = logging.getLogger(__name__)


class AssignmentHandler(BaseHTTPRequestHandler):
    """HTTP request handler for bot assignment requests."""

    def log_message(self, format, *args):
        """Override to use our logger instead of stderr."""
        logger.info("%s - %s", self.address_string(), format % args)

    def do_POST(self):
        if self.path != "/assign":
            self.send_error(404, "Not Found")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse request body: %s", e)
            self.send_error(400, f"Invalid JSON: {e}")
            return

        bot_id = data.get("bot_id")
        if bot_id is None:
            logger.warning("Received assign request without bot_id")
            self.send_error(400, "Missing bot_id in request body")
            return

        try:
            bot_id = int(bot_id)
        except (ValueError, TypeError):
            logger.warning("Invalid bot_id value: %s", bot_id)
            self.send_error(400, "bot_id must be an integer")
            return

        logger.info("Received assignment request for bot_id=%s", bot_id)

        # Store the bot_id on the server instance
        self.server.assigned_bot_id = bot_id

        # Send success response
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = json.dumps({"status": "assigned", "bot_id": bot_id})
        self.wfile.write(response.encode("utf-8"))

        # Signal the server to shut down (in a separate thread to avoid deadlock)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = json.dumps({"status": "waiting"})
            self.wfile.write(response.encode("utf-8"))
        else:
            self.send_error(404, "Not Found")


class Command(BaseCommand):
    help = "Waits for bot assignment via HTTP POST request, then runs the bot"

    # Graceful shutdown flag
    _keep_running = True
    _server = None

    def _graceful_exit(self, signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        self._keep_running = False
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def add_arguments(self, parser):
        parser.add_argument(
            "--port",
            type=int,
            default=8080,
            help="Port to listen on for assignment requests (default: 8080)",
        )
        parser.add_argument(
            "--host",
            type=str,
            default="0.0.0.0",
            help="Host to bind to (default: 0.0.0.0)",
        )

    def handle(self, *args, **options):
        host = options["host"]
        port = options["port"]

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        bot_id = self._wait_for_assignment(host, port)

        if bot_id is None:
            logger.info("Bot runner exiting without assignment")
            return

        logger.info("Running run bot task for bot %s...", bot_id)
        result = run_bot.run(bot_id)
        logger.info("Run bot task completed with result: %s", result)

    def _wait_for_assignment(self, host: str, port: int) -> int | None:
        """
        Start HTTP server and wait for assignment POST request.

        Returns the assigned bot_id when received, or None if shutdown requested.
        """
        self._server = HTTPServer((host, port), AssignmentHandler)
        self._server.assigned_bot_id = None

        logger.info("Bot runner HTTP server listening on %s:%s, waiting for assignment...", host, port)

        try:
            self._server.serve_forever()
        except Exception as e:
            logger.error("Server error: %s", e)
            return None
        finally:
            self._server.server_close()

        return self._server.assigned_bot_id
