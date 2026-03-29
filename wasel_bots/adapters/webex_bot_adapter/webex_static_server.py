import atexit
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class _WebexCOOPCOEPHandler(SimpleHTTPRequestHandler):
    # directory is set dynamically below
    directory = None

    # Whitelist of allowed files
    ALLOWED_FILES = {
        "/",
        "/index.html",
        "/app.js",
    }

    def do_GET(self):
        # Normalize path
        path = self.path.split('?')[0]  # Remove query params
        if path == "/":
            path = "/index.html"
            
        # Check if the requested file is in the whitelist
        if path not in self.ALLOWED_FILES:
            self.send_error(404, "File not found...")
            return

        # If whitelisted, proceed with normal file serving
        super().do_GET()

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


# Static server that serves the Webex SDK HTML page and adds COOP/COEP headers
def start_webex_static_server() -> int:
    """
    Start a static HTTP server to serve Webex SDK files from webex-v1 directory.

    Returns
    -------
    int
        The port number the server is running on
    """
    # Serve from wasel_bots/static/webex-v1 directory
    current_dir = os.path.dirname(os.path.abspath(__file__))
    wasel_bots_dir = os.path.dirname(os.path.dirname(current_dir))
    webex_v1_dir = os.path.join(wasel_bots_dir, "static", "webex-v1")
    
    handler_cls = partial(_WebexCOOPCOEPHandler, directory=webex_v1_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)  # 0 = choose free port
    httpd_port = httpd.server_address[1]
    httpd_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    httpd_thread.start()

    def _shutdown():
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

    # Schedule automatic shutdown after an hour if we're not on kubernetes.
    # In kubernetes, the server will be shutdown when the pod dies.
    # With celery, it will keep running after the task finishes.
    # The static file server is only used to load the page, so we could probably also
    # shut it down even if we were using kubernetes, but erring on the side of caution.
    if os.getenv("LAUNCH_BOT_METHOD") != "kubernetes":
        timeout_seconds = 60 * 60  # 1 hour
        shutdown_timer = threading.Timer(timeout_seconds, _shutdown)
        shutdown_timer.daemon = True
        shutdown_timer.start()

    atexit.register(_shutdown)
    print(f"Started Webex static server on port {httpd_port}")
    return httpd_port

