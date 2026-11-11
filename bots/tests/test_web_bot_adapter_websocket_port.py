import socket
import threading
import time
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter


class WebsocketPortSelectionTest(SimpleTestCase):
    def start_server(self, fixed_port):
        adapter = SimpleNamespace(
            websocket_port=None,
            websocket_host=None,
            websocket_server=None,
            livekit_websocket_bridge=None,
            handle_websocket=lambda websocket: None,
            get_websocket_port=lambda: fixed_port,
        )
        threading.Thread(target=WebBotAdapter.run_websocket_server, args=(adapter,), daemon=True).start()
        deadline = time.time() + 5
        while adapter.websocket_port is None and time.time() < deadline:
            time.sleep(0.01)
        self.addCleanup(lambda: adapter.websocket_server and adapter.websocket_server.shutdown())
        return adapter

    def free_port(self):
        with socket.socket() as s:
            s.bind(("localhost", 0))
            return s.getsockname()[1]

    @override_settings(ENABLE_BOT_WEBSOCKET_PORT_SELECTION_BY_OS=False)
    def test_uses_fixed_port_by_default(self):
        port = self.free_port()
        adapter = self.start_server(port)
        self.assertEqual(adapter.websocket_port, port)
        self.assertIsNone(adapter.websocket_host)

    @override_settings(ENABLE_BOT_WEBSOCKET_PORT_SELECTION_BY_OS=True)
    def test_bots_get_distinct_loopback_ips_on_same_port_when_enabled(self):
        port = self.free_port()
        adapters = [self.start_server(port) for _ in range(12)]
        self.assertEqual({adapter.websocket_port for adapter in adapters}, {port})
        hosts = {adapter.websocket_host for adapter in adapters}
        self.assertNotIn(None, hosts)
        self.assertEqual(len(hosts), 12)
        for host in hosts:
            self.assertTrue(host.startswith("127."))
            socket.create_connection((host, port), timeout=2).close()
