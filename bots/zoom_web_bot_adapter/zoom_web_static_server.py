import atexit
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class _COOPCOEPHandler(SimpleHTTPRequestHandler):
    # directory is set dynamically below
    directory = None

    # Whitelist of allowed files
    ALLOWED_FILES = {
        "/zoom_web_chromedriver_page.html",
        "/zoom_web_chromedriver_page.js",
        "/zoom_web_chromedriver_style.css",
    }

    def do_GET(self):
        # Check if the requested file is in the whitelist
        if self.path not in self.ALLOWED_FILES:
            self.send_error(404, "File not found...")
            return

        # If whitelisted, proceed with normal file serving
        super().do_GET()

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


# Super simple static server that serves the zoom web sdk HTML page and adds COOP/COEP headers to enable gallery view
def start_zoom_web_static_server() -> int:
    # Bind the directory at construction time (correct way for 3.8+)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    handler_cls = partial(_COOPCOEPHandler, directory=current_dir)
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
    # The static file server is only used for to load the page, so we could probably also
    # shut it down even if we were using kubernetes, but erroring on the side of caution.
    if os.getenv("LAUNCH_BOT_METHOD") != "kubernetes":
        timeout_seconds = 60 * 60  # 1 hour
        shutdown_timer = threading.Timer(timeout_seconds, _shutdown)
        shutdown_timer.daemon = True
        shutdown_timer.start()

    atexit.register(_shutdown)
    return httpd_port
