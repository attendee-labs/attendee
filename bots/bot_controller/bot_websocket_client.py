import json
import logging
import time
from queue import Empty, SimpleQueue
from threading import Lock, Thread
from typing import Callable

from websockets import ConnectionClosed
from websockets.sync.client import connect

logger = logging.getLogger(__name__)


class BotWebsocketClient:
    """
    A websocket loop that sends and receives messages to/from a websocket server
    Designed to be used by a BotController to control the bot audio/video
    remotely.
    """

    # Connection-state values
    NOT_STARTED = "NOT_STARTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"

    def __init__(self, url: str, on_message_callback: Callable[[dict], None]):
        self.on_message_callback = on_message_callback
        self.websocket_url = url
        self.websocket = None

        self.connection_state = self.NOT_STARTED
        self.connection_thread = None
        self.recv_loop_thread = None
        self.send_loop_thread = None
        self.send_queue = SimpleQueue()

        self._max_retries = 30
        self._retry_delay_s = 10
        self.dropped_message_ticker = 0
        self._start_connection_lock = Lock()

    # --------------------------------------------------------------------- #
    #  Public helpers                                                       #
    # --------------------------------------------------------------------- #

    def started(self):
        return self.connection_state != self.NOT_STARTED

    def start(self):
        logger.info(f"Starting BotWebsocketClient for url {self.websocket_url}")
        self._start_connection_thread()

    def cleanup(self):
        logger.info("Stopping BotWebsocketClient")
        try:
            if self.websocket:
                self.websocket.close()
        except Exception as e:
            logger.error("Error closing BotWebsocketClient websocket: %s", e)
        finally:
            self.connection_state = self.STOPPED

    def send_async(self, message: dict):
        if self.connection_state == self.CONNECTED:
            self.send_queue.put(message)
        else:
            if self.dropped_message_ticker % 1000 == 0:
                logger.warning("BotWebsocketClient is not connected, it is in state %s, dropping message", self.connection_state)
            self.dropped_message_ticker += 1

    # --------------------------------------------------------------------- #
    #  Internal helpers                                                     #
    # --------------------------------------------------------------------- #
    def _start_connection_thread(self):
        with self._start_connection_lock:
            if self.connection_state == self.CONNECTING:
                logger.info("BotWebsocketClient connection thread already running")
                return
            if self.connection_thread and self.connection_thread.is_alive():
                logger.info("BotWebsocketClient connection thread already running")
                return
            self.connection_state = self.CONNECTING
            self.connection_thread = Thread(target=self._connection_loop, daemon=True)
            self.connection_thread.start()

    def _connection_loop(self):
        retries = 0
        while self.connection_state == self.CONNECTING and retries < self._max_retries:
            try:
                self.websocket = connect(self.websocket_url)

                logger.info("BotWebsocketClient websocket connected, waiting for worker threads to finish")

                # if the worker threads are running, wait for them to finish
                if self.recv_loop_thread and self.recv_loop_thread.is_alive():
                    self.recv_loop_thread.join()
                if self.send_loop_thread and self.send_loop_thread.is_alive():
                    self.send_loop_thread.join()

                self.connection_state = self.CONNECTED
                logger.info("BotWebsocketClient websocket connected, launching worker threads")

                # Launch worker threads (fresh each time we reconnect)
                self.recv_loop_thread = Thread(target=self.recv_loop, daemon=True)
                self.send_loop_thread = Thread(target=self.send_loop, daemon=True)
                self.recv_loop_thread.start()
                self.send_loop_thread.start()
                return  # success – leave the loop
            except Exception as e:
                retries += 1
                logger.warning(
                    "BotWebsocketClient connection attempt %d/%d failed: %s",
                    retries,
                    self._max_retries,
                    e,
                )
                time.sleep(self._retry_delay_s)

        # Handle case where we were stopped before we could connect
        if self.connection_state != self.CONNECTING:
            logger.info("BotWebsocketClient connection loop exited because connection state is %s", self.connection_state)
            return

        # Exhausted retries
        self.connection_state = self.FAILED
        logger.error("BotWebsocketClient failed to establish websocket connection after %d retries", self._max_retries)

    def _trigger_reconnect(self):
        if self.connection_state in [self.CONNECTING, self.FAILED, self.STOPPED]:
            logger.info("BotWebsocketClient aborting websocket reconnect because connection state is %s", self.connection_state)
            return  # already trying or permanently failed
        logger.info("BotWebsocketClient triggering websocket reconnect")
        self._start_connection_thread()

    # --------------------------------------------------------------------- #
    #  Worker threads                                                       #
    # --------------------------------------------------------------------- #
    def send_loop(self):
        logger.info("BotWebsocketClient send loop started")
        while self.connection_state == self.CONNECTED:
            try:
                message = self.send_queue.get(timeout=1)
            except Empty:
                continue  # nothing queued yet

            try:
                self.websocket.send(json.dumps(message))
            except Exception as e:
                logger.info("BotWebsocketClient send failed (%s). Leaving loop.", e)
                break

        logger.info("BotWebsocketClient send loop exited")
        self._trigger_reconnect()

    def recv_loop(self):
        logger.info("BotWebsocketClient recv loop started")
        while self.connection_state == self.CONNECTED:
            try:
                message = self.websocket.recv()
                self.on_message_callback(message)
            except ConnectionClosed:
                logger.info("BotWebsocketClient connection closed. Leaving loop.")
                break
            except Exception as e:
                logger.info("BotWebsocketClient recv failed (%s). Leaving loop.", e)
                break

        logger.info("BotWebsocketClient recv loop exited")
        self._trigger_reconnect()
