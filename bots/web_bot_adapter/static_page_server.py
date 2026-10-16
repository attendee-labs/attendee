import atexit
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def start_static_page_server(directory: str, allowed_paths: set[str], extra_headers: dict[str, str] | None = None) -> int:
    """Serve the allowed files of a directory on a free local port and return the port.

    Platforms that load their SDK in a page need an http origin to load it from, so the adapter serves that page from here.
    """

    class PageRequestHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path not in allowed_paths:
                self.send_error(404, "File not found...")
                return
            super().do_GET()

        def end_headers(self):
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            super().end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(PageRequestHandler, directory=directory))  # 0 = choose free port
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
    return httpd.server_address[1]
