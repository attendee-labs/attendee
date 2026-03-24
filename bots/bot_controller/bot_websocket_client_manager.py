import logging
from typing import Callable

from bots.bot_controller.bot_websocket_client import BotWebsocketClient

logger = logging.getLogger(__name__)


class BotWebsocketClientManager:
    """
    Manages BotWebsocketClient instances for mixed and per-participant audio
    websocket streams. When both URLs are identical, a single underlying client
    is shared to avoid duplicate connections. Callers just call send_mixed_audio
    / send_per_participant_audio and this class handles client lifecycle.
    """

    def __init__(
        self,
        mixed_audio_url: str | None,
        per_participant_audio_url: str | None,
        on_message_callback: Callable[[dict], None],
    ):
        def add_purpose(url: str, purpose: str):
            if not url:
                return
            if url not in self._url_to_purposes:
                self._url_to_purposes[url] = []
            self._url_to_purposes[url].append(purpose)

        def get_or_create_client(url: str | None, purpose: str) -> BotWebsocketClient | None:
            if not url:
                return None
            if url not in client_by_url:
                client_by_url[url] = BotWebsocketClient(url=url, on_message_callback=on_message_callback)
            add_purpose(url, purpose)
            return client_by_url[url]

        client_by_url: dict[str, BotWebsocketClient] = {}
        self._url_to_purposes: dict[str, list[str]] = {}
        self._mixed_audio_client = get_or_create_client(mixed_audio_url, "mixed_audio")
        self._per_participant_audio_client = get_or_create_client(per_participant_audio_url, "per_participant_audio")
        self._clients = list(client_by_url.values())

    def _ensure_started(self, client: BotWebsocketClient):
        if not client.started():
            logger.info("Starting websocket client for %s...", ", ".join(self._url_to_purposes[client.websocket_url]))
            client.start()

    def send_mixed_audio(self, payload: dict):
        if not self._mixed_audio_client:
            return
        self._ensure_started(self._mixed_audio_client)
        self._mixed_audio_client.send_async(payload)

    def send_per_participant_audio(self, payload: dict):
        if not self._per_participant_audio_client:
            return
        self._ensure_started(self._per_participant_audio_client)
        self._per_participant_audio_client.send_async(payload)

    def cleanup(self):
        for client in self._clients:
            client.cleanup()
