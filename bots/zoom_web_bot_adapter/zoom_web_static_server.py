import os

from bots.web_bot_adapter.static_page_server import start_static_page_server

ALLOWED_PATHS = {
    "/zoom_web_chromedriver_page.html",
    "/zoom_web_chromedriver_page.js",
    "/zoom_web_chromedriver_style.css",
}

# The COOP and COEP headers enable gallery view
COOP_COEP_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}


# Super simple static server that serves the zoom web sdk HTML page and adds COOP/COEP headers to enable gallery view
def start_zoom_web_static_server() -> int:
    return start_static_page_server(os.path.dirname(os.path.abspath(__file__)), ALLOWED_PATHS, COOP_COEP_HEADERS)
