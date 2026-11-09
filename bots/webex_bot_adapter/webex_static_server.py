import atexit
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class _WebexPageHandler(SimpleHTTPRequestHandler):
    # Whitelist of allowed files
    ALLOWED_FILES = {
        "/webex_chromedriver_page.html",
        "/webex_chromedriver_page.js",
    }

    def do_GET(self):
        if self.path not in self.ALLOWED_FILES:
            self.send_error(404, "File not found...")
            return

        super().do_GET()


# Super simple static server that serves the webex sdk HTML page
def start_webex_static_server() -> int:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    handler_cls = partial(_WebexPageHandler, directory=current_dir)
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
    if os.getenv("LAUNCH_BOT_METHOD") != "kubernetes":
        timeout_seconds = 60 * 60  # 1 hour
        shutdown_timer = threading.Timer(timeout_seconds, _shutdown)
        shutdown_timer.daemon = True
        shutdown_timer.start()

    atexit.register(_shutdown)
    return httpd_port
