import json
import threading
import time
import unittest
from queue import Empty
from unittest.mock import Mock, call, patch

from websockets import ConnectionClosed

from bots.bot_controller.bot_websocket_client import BotWebsocketClient


class TestBotWebsocketClient(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.test_url = "ws://test.example.com"
        self.mock_callback = Mock()
        self.client = BotWebsocketClient(self.test_url, self.mock_callback)

    def tearDown(self):
        """Clean up after each test method."""
        if self.client:
            self.client.cleanup()

    # --------------------------------------------------------------------- #
    #  Initialization tests                                                 #
    # --------------------------------------------------------------------- #

    def test_initialization(self):
        """Test that BotWebsocketClient initializes with correct default values."""
        self.assertEqual(self.client.websocket_url, self.test_url)
        self.assertEqual(self.client.on_message_callback, self.mock_callback)
        self.assertEqual(self.client.connection_state, BotWebsocketClient.NOT_STARTED)
        self.assertIsNone(self.client.websocket)
        self.assertIsNone(self.client.connection_thread)
        self.assertIsNone(self.client.recv_loop_thread)
        self.assertIsNone(self.client.send_loop_thread)
        self.assertEqual(self.client.dropped_message_ticker, 0)
        self.assertEqual(self.client._max_retries, 30)
        self.assertEqual(self.client._retry_delay_s, 10)

    def test_started_method(self):
        """Test the started() method returns correct values."""
        # Initially not started
        self.assertFalse(self.client.started())

        # After changing state to anything but NOT_STARTED
        self.client.connection_state = BotWebsocketClient.CONNECTING
        self.assertTrue(self.client.started())

        self.client.connection_state = BotWebsocketClient.CONNECTED
        self.assertTrue(self.client.started())

        self.client.connection_state = BotWebsocketClient.FAILED
        self.assertTrue(self.client.started())

        self.client.connection_state = BotWebsocketClient.STOPPED
        self.assertTrue(self.client.started())

    # --------------------------------------------------------------------- #
    #  Connection and threading tests                                       #
    # --------------------------------------------------------------------- #

    @patch("bots.bot_controller.bot_websocket_client.connect")
    def test_start_successful_connection(self, mock_connect):
        """Test successful websocket connection."""
        mock_websocket = Mock()
        mock_connect.return_value = mock_websocket

        # Start the client
        self.client.start()

        # Wait a moment for the connection thread to work
        time.sleep(0.5)

        # Check that connection was attempted
        mock_connect.assert_called_with(self.test_url)
        self.assertEqual(self.client.connection_state, BotWebsocketClient.CONNECTED)
        self.assertEqual(self.client.websocket, mock_websocket)

        # Check that worker threads were started
        self.assertIsNotNone(self.client.recv_loop_thread)
        self.assertIsNotNone(self.client.send_loop_thread)
        self.assertTrue(self.client.recv_loop_thread.is_alive())
        self.assertTrue(self.client.send_loop_thread.is_alive())

    @patch("bots.bot_controller.bot_websocket_client.connect")
    @patch("bots.bot_controller.bot_websocket_client.time.sleep")
    def test_connection_retries_on_failure(self, mock_sleep, mock_connect):
        """Test that connection retries on failure."""
        # Make connection fail a few times then succeed
        mock_websocket = Mock()
        mock_connect.side_effect = [ConnectionError("Connection failed"), ConnectionError("Connection failed"), mock_websocket]

        self.client.start()

        # Wait for connection thread to complete
        if self.client.connection_thread:
            self.client.connection_thread.join(timeout=2)

        # Should have tried 3 times total
        self.assertEqual(mock_connect.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # Sleep between retries (not after last success)
        self.assertEqual(self.client.connection_state, BotWebsocketClient.CONNECTED)

    @patch("bots.bot_controller.bot_websocket_client.connect")
    @patch("bots.bot_controller.bot_websocket_client.time.sleep")
    def test_connection_fails_after_max_retries(self, mock_sleep, mock_connect):
        """Test that connection fails after max retries are exhausted."""
        mock_connect.side_effect = ConnectionError("Connection failed")

        # Set low max retries for faster test
        self.client._max_retries = 3
        self.client._retry_delay_s = 0.01

        self.client.start()

        # Wait for connection thread to complete
        if self.client.connection_thread:
            self.client.connection_thread.join(timeout=2)

        # Should have tried max_retries times
        self.assertEqual(mock_connect.call_count, 3)
        self.assertEqual(self.client.connection_state, BotWebsocketClient.FAILED)

    def test_start_connection_thread_already_running(self):
        """Test that starting when already connecting doesn't create duplicate threads."""
        # Test case 1: When state is already CONNECTING
        with patch("bots.bot_controller.bot_websocket_client.Thread") as mock_thread_class:
            mock_thread = Mock()
            mock_thread_class.return_value = mock_thread

            # Set state to CONNECTING
            self.client.connection_state = BotWebsocketClient.CONNECTING

            # Call start multiple times in parallel
            threads = []
            for _ in range(3):
                t = threading.Thread(target=self.client.start)
                threads.append(t)
                t.start()

            # Wait for all threads to complete
            for t in threads:
                t.join()

            # Should not create any threads because state is already CONNECTING
            mock_thread_class.assert_not_called()

    def test_start_connection_thread_with_alive_thread(self):
        """Test that starting when connection thread is alive doesn't create duplicate threads."""
        # Test case 2: When connection_thread exists and is alive
        with patch("bots.bot_controller.bot_websocket_client.Thread") as mock_thread_class:
            mock_thread = Mock()
            mock_thread.is_alive.return_value = True  # Thread is alive
            mock_thread_class.return_value = mock_thread

            # Set up existing alive thread
            self.client.connection_thread = mock_thread
            self.client.connection_state = BotWebsocketClient.NOT_STARTED

            # Call start multiple times in parallel
            threads = []
            for _ in range(2):
                t = threading.Thread(target=self.client.start)
                threads.append(t)
                t.start()

            # Wait for all threads to complete
            for t in threads:
                t.join()

            # Should not create any new threads because existing thread is alive
            mock_thread_class.assert_not_called()

    def test_start_connection_thread_creates_new_when_appropriate(self):
        """Test that start creates a new thread when appropriate."""
        with patch("bots.bot_controller.bot_websocket_client.Thread") as mock_thread_class:
            mock_thread = Mock()
            mock_thread_class.return_value = mock_thread

            # State is NOT_STARTED and no existing thread
            self.client.connection_state = BotWebsocketClient.NOT_STARTED
            self.client.connection_thread = None

            # Call start multiple times in parallel
            threads = []
            for _ in range(3):
                t = threading.Thread(target=self.client.start)
                threads.append(t)
                t.start()

            # Wait for all threads to complete
            for t in threads:
                t.join()

            # Should create exactly one thread
            mock_thread_class.assert_called_once_with(target=self.client._connection_loop, daemon=True)
            mock_thread.start.assert_called_once()
            self.assertEqual(self.client.connection_state, BotWebsocketClient.CONNECTING)

    def test_start_connection_thread_replaces_dead_thread(self):
        """Test that start creates a new thread when existing thread is dead."""
        with patch("bots.bot_controller.bot_websocket_client.Thread") as mock_thread_class:
            mock_new_thread = Mock()
            mock_thread_class.return_value = mock_new_thread

            # Set up existing dead thread
            mock_dead_thread = Mock()
            mock_dead_thread.is_alive.return_value = False  # Thread is dead
            self.client.connection_thread = mock_dead_thread
            self.client.connection_state = BotWebsocketClient.NOT_STARTED

            self.client.start()

            # Should create exactly one new thread
            mock_thread_class.assert_called_once_with(target=self.client._connection_loop, daemon=True)
            mock_new_thread.start.assert_called_once()
            self.assertEqual(self.client.connection_state, BotWebsocketClient.CONNECTING)
            # Should replace the old thread
            self.assertEqual(self.client.connection_thread, mock_new_thread)

    # --------------------------------------------------------------------- #
    #  Message sending tests                                                #
    # --------------------------------------------------------------------- #

    def test_send_async_when_connected(self):
        """Test sending messages when connected."""
        self.client.connection_state = BotWebsocketClient.CONNECTED
        test_message = {"type": "test", "data": "hello"}

        self.client.send_async(test_message)

        # Message should be in the queue
        queued_message = self.client.send_queue.get_nowait()
        self.assertEqual(queued_message, test_message)

    def test_send_async_when_not_connected(self):
        """Test that messages are dropped when not connected."""
        self.client.connection_state = BotWebsocketClient.NOT_STARTED
        test_message = {"type": "test", "data": "hello"}

        with patch("bots.bot_controller.bot_websocket_client.logger") as mock_logger:
            self.client.send_async(test_message)

            # Message should not be in queue
            with self.assertRaises(Empty):
                self.client.send_queue.get_nowait()

            # Should increment dropped message ticker
            self.assertEqual(self.client.dropped_message_ticker, 1)

            # Should log warning
            mock_logger.warning.assert_called_once()

    def test_send_loop(self):
        """Test the send loop functionality."""
        mock_websocket = Mock()
        self.client.websocket = mock_websocket
        self.client.connection_state = BotWebsocketClient.CONNECTED

        # Add test messages to queue
        test_messages = [{"type": "test1", "data": "hello"}, {"type": "test2", "data": "world"}]

        for msg in test_messages:
            self.client.send_queue.put(msg)

        # Disconnect after processing messages
        def side_effect(*args):
            if self.client.send_queue.empty():
                self.client.connection_state = BotWebsocketClient.STOPPED

        mock_websocket.send.side_effect = side_effect

        # Run send loop
        self.client.send_loop()

        # Check that messages were sent
        expected_calls = [call(json.dumps(msg)) for msg in test_messages]
        mock_websocket.send.assert_has_calls(expected_calls)

    def test_send_loop_handles_send_error(self):
        """Test that send loop handles websocket send errors."""
        mock_websocket = Mock()
        mock_websocket.send.side_effect = ConnectionError("Send failed")
        self.client.websocket = mock_websocket
        self.client.connection_state = BotWebsocketClient.CONNECTED

        # Add a test message
        self.client.send_queue.put({"type": "test"})

        with patch.object(self.client, "_trigger_reconnect") as mock_reconnect:
            self.client.send_loop()
            mock_reconnect.assert_called_once()

    # --------------------------------------------------------------------- #
    #  Message receiving tests                                              #
    # --------------------------------------------------------------------- #

    def test_recv_loop(self):
        """Test the receive loop functionality."""
        mock_websocket = Mock()
        test_messages = ['{"type": "msg1"}', '{"type": "msg2"}']

        # Mock websocket to return messages then raise ConnectionClosed
        mock_websocket.recv.side_effect = test_messages + [ConnectionClosed(None, None)]

        self.client.websocket = mock_websocket
        self.client.connection_state = BotWebsocketClient.CONNECTED

        with patch.object(self.client, "_trigger_reconnect") as mock_reconnect:
            self.client.recv_loop()

            # Check that callback was called for each message
            expected_calls = [call(msg) for msg in test_messages]
            self.mock_callback.assert_has_calls(expected_calls)

            # Should trigger reconnect after connection closed
            mock_reconnect.assert_called_once()

    def test_recv_loop_handles_recv_error(self):
        """Test that recv loop handles websocket receive errors."""
        mock_websocket = Mock()
        mock_websocket.recv.side_effect = ConnectionError("Recv failed")

        self.client.websocket = mock_websocket
        self.client.connection_state = BotWebsocketClient.CONNECTED

        with patch.object(self.client, "_trigger_reconnect") as mock_reconnect:
            self.client.recv_loop()
            mock_reconnect.assert_called_once()

    # --------------------------------------------------------------------- #
    #  Reconnection tests                                                   #
    # --------------------------------------------------------------------- #

    def test_trigger_reconnect_when_connected(self):
        """Test triggering reconnect when currently connected."""
        self.client.connection_state = BotWebsocketClient.CONNECTED

        with patch.object(self.client, "_start_connection_thread") as mock_start:
            self.client._trigger_reconnect()
            mock_start.assert_called_once()

    def test_trigger_reconnect_aborts_when_inappropriate(self):
        """Test that reconnect is aborted when in inappropriate states."""
        inappropriate_states = [BotWebsocketClient.CONNECTING, BotWebsocketClient.FAILED, BotWebsocketClient.STOPPED]

        for state in inappropriate_states:
            with self.subTest(state=state):
                self.client.connection_state = state

                with patch.object(self.client, "_start_connection_thread") as mock_start:
                    self.client._trigger_reconnect()
                    mock_start.assert_not_called()

    # --------------------------------------------------------------------- #
    #  Cleanup tests                                                        #
    # --------------------------------------------------------------------- #

    def test_cleanup(self):
        """Test cleanup properly closes websocket and sets state."""
        mock_websocket = Mock()
        self.client.websocket = mock_websocket

        self.client.cleanup()

        mock_websocket.close.assert_called_once()
        self.assertEqual(self.client.connection_state, BotWebsocketClient.STOPPED)

    def test_cleanup_handles_close_error(self):
        """Test cleanup handles websocket close errors gracefully."""
        mock_websocket = Mock()
        mock_websocket.close.side_effect = ConnectionError("Close failed")
        self.client.websocket = mock_websocket

        with patch("bots.bot_controller.bot_websocket_client.logger") as mock_logger:
            self.client.cleanup()

            # Should log error but still set state to STOPPED
            mock_logger.error.assert_called_once()
            self.assertEqual(self.client.connection_state, BotWebsocketClient.STOPPED)

    def test_cleanup_when_no_websocket(self):
        """Test cleanup when websocket is None."""
        self.client.websocket = None

        # Should not raise exception
        self.client.cleanup()
        self.assertEqual(self.client.connection_state, BotWebsocketClient.STOPPED)

    # --------------------------------------------------------------------- #
    #  Integration-style tests                                              #
    # --------------------------------------------------------------------- #

    @patch("bots.bot_controller.bot_websocket_client.connect")
    def test_full_lifecycle(self, mock_connect):
        """Test a full connection lifecycle."""
        mock_websocket = Mock()
        mock_connect.return_value = mock_websocket

        # Start connection
        self.client.start()
        time.sleep(0.1)

        # Should be connected
        self.assertEqual(self.client.connection_state, BotWebsocketClient.CONNECTED)

        # Send a message
        test_message = {"type": "test", "data": "hello"}
        self.client.send_async(test_message)

        # Wait for send loop to process
        time.sleep(0.1)

        # Message should have been sent
        mock_websocket.send.assert_called_with(json.dumps(test_message))

        # Cleanup
        self.client.cleanup()
        self.assertEqual(self.client.connection_state, BotWebsocketClient.STOPPED)
        mock_websocket.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
