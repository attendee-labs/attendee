import logging
import threading
from urllib.parse import urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as websocket_connect

logger = logging.getLogger(__name__)


class LiveKitWebsocketBridge:
    """Relays LiveKit signalling websocket traffic between the in-browser
    LiveKit client and the real LiveKit server.

    In MS Teams, the page's CSP blocks the browser from connecting to the LiveKit host
    directly, so the in-browser client connects to the bot's local websocket
    server on paths under /rtc instead. This bridge forwards those
    connections (path and query string intact) to the configured LiveKit host.

    This class is only instantiated when room sync via LiveKit is configured.
    In the normal case it does not exist and the adapter's websocket handling
    is completely unchanged.
    """

    BRIDGE_PATH_PREFIX = "/rtc"

    def __init__(self, livekit_url: str):
        self.livekit_url = livekit_url

    # -- routing helpers ----------------------------------------------------

    @staticmethod
    def get_websocket_request_path(websocket):
        # websockets >= 13 exposes the request path on websocket.request.path.
        # Keep websocket.path as a fallback for older versions.
        request = getattr(websocket, "request", None)
        if request is not None:
            request_path = getattr(request, "path", None)
            if request_path:
                return request_path

        return getattr(websocket, "path", "/")

    @classmethod
    def is_bridge_path(cls, request_path) -> bool:
        return request_path == cls.BRIDGE_PATH_PREFIX or request_path.startswith(cls.BRIDGE_PATH_PREFIX + "/")

    # -- internals ----------------------------------------------------------

    def build_upstream_websocket_url(self, request_path):
        parsed_livekit_url = urlparse(self.livekit_url)

        if parsed_livekit_url.scheme not in ("ws", "wss") or not parsed_livekit_url.netloc:
            raise ValueError(f"Invalid LiveKit WebSocket URL: {self.livekit_url!r}")

        # LiveKit's Room.connect() expects a base server URL. The SDK appends
        # /rtc/... and its query string; the bridge forwards exactly that path
        # and query to the configured LiveKit host.
        if parsed_livekit_url.path not in ("", "/") or parsed_livekit_url.params or parsed_livekit_url.query or parsed_livekit_url.fragment:
            raise ValueError("LiveKit WebSocket URL must be a base URL without a path, query string, or fragment")

        if not self.is_bridge_path(request_path):
            raise ValueError(f"Unexpected LiveKit WebSocket path: {request_path!r}")

        return f"{parsed_livekit_url.scheme}://{parsed_livekit_url.netloc}{request_path}"

    def close_websocket_from_peer(self, websocket, peer_websocket):
        close_code = getattr(peer_websocket, "close_code", None)
        close_reason = getattr(peer_websocket, "close_reason", "") or ""

        # 1005, 1006, and 1015 are reserved and cannot be sent in a Close frame.
        if close_code in (None, 1005, 1006, 1015):
            close_code = 1011 if close_code == 1006 else 1000

        try:
            websocket.close(code=close_code, reason=close_reason)
        except Exception:
            pass

    # -- entry point --------------------------------------------------------

    def handle(self, browser_websocket, request_path):
        upstream_url = self.build_upstream_websocket_url(request_path)
        request_path_without_query = request_path.split("?", 1)[0]

        logger.info(
            "LiveKit WebSocket bridge connecting local path %s to %s",
            request_path_without_query,
            self.livekit_url,
        )

        upstream_websocket = None
        upstream_to_browser_thread = None

        try:
            upstream_websocket = websocket_connect(
                upstream_url,
                compression=None,
                max_size=None,
            )

            def relay_upstream_to_browser():
                try:
                    for message in upstream_websocket:
                        browser_websocket.send(message)
                except ConnectionClosed:
                    pass
                except Exception as e:
                    logger.warning(f"LiveKit WebSocket bridge upstream-to-browser error: {e}")
                finally:
                    self.close_websocket_from_peer(browser_websocket, upstream_websocket)

            upstream_to_browser_thread = threading.Thread(
                target=relay_upstream_to_browser,
                daemon=True,
            )
            upstream_to_browser_thread.start()

            try:
                for message in browser_websocket:
                    upstream_websocket.send(message)
            except ConnectionClosed:
                pass
        except ConnectionClosed:
            pass
        except Exception as e:
            logger.exception(f"LiveKit WebSocket bridge error: {e}")
        finally:
            if upstream_websocket is not None:
                self.close_websocket_from_peer(upstream_websocket, browser_websocket)

            if upstream_to_browser_thread is not None and upstream_to_browser_thread.is_alive():
                upstream_to_browser_thread.join(timeout=1)

            logger.info("LiveKit WebSocket bridge connection closed")
