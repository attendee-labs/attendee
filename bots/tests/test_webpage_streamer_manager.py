import os
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from bots.bot_controller.webpage_streamer_manager import WebpageStreamerManager


class TestWebpageStreamerManager(unittest.TestCase):
    def setUp(self):
        """Set up common test fixtures."""
        self.get_peer_connection_offer_callback = MagicMock(return_value={"sdp": "test_sdp", "type": "offer"})
        self.start_peer_connection_callback = MagicMock()
        self.play_bot_output_media_stream_callback = MagicMock()
        self.stop_bot_output_media_stream_callback = MagicMock()
        self.on_message_that_webpage_streamer_connection_can_start_callback = MagicMock()
        self.webpage_streamer_service_hostname = "test-hostname"

        self.manager = WebpageStreamerManager(
            get_peer_connection_offer_callback=self.get_peer_connection_offer_callback,
            start_peer_connection_callback=self.start_peer_connection_callback,
            play_bot_output_media_stream_callback=self.play_bot_output_media_stream_callback,
            stop_bot_output_media_stream_callback=self.stop_bot_output_media_stream_callback,
            on_message_that_webpage_streamer_connection_can_start_callback=self.on_message_that_webpage_streamer_connection_can_start_callback,
            webpage_streamer_service_hostname=self.webpage_streamer_service_hostname,
        )

    def test_initialization(self):
        """Test initialization of WebpageStreamerManager."""
        self.assertIsNone(self.manager.url)
        self.assertIsNone(self.manager.last_non_empty_url)
        self.assertIsNone(self.manager.output_destination)
        self.assertEqual(self.manager.get_peer_connection_offer_callback, self.get_peer_connection_offer_callback)
        self.assertEqual(self.manager.start_peer_connection_callback, self.start_peer_connection_callback)
        self.assertFalse(self.manager.cleaned_up)
        self.assertIsNone(self.manager.keepalive_task)
        self.assertEqual(self.manager.webpage_streamer_service_hostname, self.webpage_streamer_service_hostname)
        self.assertEqual(self.manager.play_bot_output_media_stream_callback, self.play_bot_output_media_stream_callback)
        self.assertEqual(self.manager.stop_bot_output_media_stream_callback, self.stop_bot_output_media_stream_callback)
        self.assertEqual(self.manager.on_message_that_webpage_streamer_connection_can_start_callback, self.on_message_that_webpage_streamer_connection_can_start_callback)
        self.assertFalse(self.manager.webrtc_connection_started)
        self.assertFalse(self.manager.webpage_streamer_has_started)

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    def test_init_method(self, mock_thread):
        """Test init method starts keepalive task."""
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        self.manager.init()

        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()
        self.assertIsNotNone(self.manager.keepalive_task)

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    def test_init_method_called_twice(self, mock_thread):
        """Test init method doesn't start keepalive task twice."""
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        self.manager.init()
        self.manager.init()

        # Should only call thread once
        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()

    def test_streaming_service_hostname_kubernetes(self):
        """Test streaming_service_hostname returns correct hostname for kubernetes."""
        with patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"}):
            hostname = self.manager.streaming_service_hostname()
            self.assertEqual(hostname, "test-hostname")

    def test_streaming_service_hostname_docker_compose(self):
        """Test streaming_service_hostname returns correct hostname for docker compose."""
        with patch.dict(os.environ, {}, clear=True):
            hostname = self.manager.streaming_service_hostname()
            self.assertEqual(hostname, "attendee-webpage-streamer-local")

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_first_time_with_url(self, mock_post, mock_thread):
        """Test update when streaming hasn't started yet - should return early."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response]

        # When webpage_streamer_has_started is False, update should return early
        self.manager.update("https://example.com", "screenshare")

        # Nothing should be updated or called because it returned early
        self.assertIsNone(self.manager.url)
        self.assertIsNone(self.manager.output_destination)
        self.assertIsNone(self.manager.last_non_empty_url)
        self.get_peer_connection_offer_callback.assert_not_called()
        self.start_peer_connection_callback.assert_not_called()
        self.play_bot_output_media_stream_callback.assert_not_called()
        self.assertFalse(self.manager.webrtc_connection_started)

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_after_streamer_started(self, mock_post, mock_thread):
        """Test update after webpage streamer has started."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response]

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        self.manager.update("https://example.com", "screenshare")

        self.assertEqual(self.manager.url, "https://example.com")
        self.assertEqual(self.manager.output_destination, "screenshare")
        self.assertEqual(self.manager.last_non_empty_url, "https://example.com")
        self.get_peer_connection_offer_callback.assert_called_once()
        self.start_peer_connection_callback.assert_called_once_with({"answer": "test_answer"})
        self.play_bot_output_media_stream_callback.assert_called_once_with("screenshare")
        self.assertTrue(self.manager.webrtc_connection_started)

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_url_changed(self, mock_post, mock_thread):
        """Test update when URL has changed."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        # First setup initial state
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_update_response = Mock()
        mock_update_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response, mock_update_response]

        self.manager.update("https://example.com", "screenshare")

        # Now change the URL
        self.manager.update("https://newurl.com", "screenshare")

        self.assertEqual(self.manager.url, "https://newurl.com")
        self.assertEqual(self.manager.last_non_empty_url, "https://newurl.com")
        # Should call update_webrtc_connection
        self.assertEqual(mock_post.call_count, 3)

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_output_destination_changed(self, mock_post, mock_sleep, mock_thread):
        """Test update when output destination has changed."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        # First setup initial state
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response]

        self.manager.update("https://example.com", "screenshare")

        # Now change the output destination
        self.manager.update("https://example.com", "webcam")

        self.assertEqual(self.manager.output_destination, "webcam")
        self.stop_bot_output_media_stream_callback.assert_called_once()
        # Should call play with new destination
        self.assertEqual(self.play_bot_output_media_stream_callback.call_count, 2)
        self.play_bot_output_media_stream_callback.assert_called_with("webcam")
        mock_sleep.assert_called_once_with(1)

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_url_and_destination_changed(self, mock_post, mock_sleep, mock_thread):
        """Test update when both URL and output destination have changed."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        # First setup initial state
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_update_response = Mock()
        mock_update_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response, mock_update_response]

        self.manager.update("https://example.com", "webcam")
        self.manager.update("https://example.com", "screenshare")

        # Now change both
        self.manager.update("https://newurl.com", "webcam")

        self.assertEqual(self.manager.url, "https://newurl.com")
        self.assertEqual(self.manager.output_destination, "webcam")
        # Should be called twice: once for the initial state, once for the change
        self.assertListEqual(self.stop_bot_output_media_stream_callback.call_args_list, [call(), call()])
        self.assertEqual(self.play_bot_output_media_stream_callback.call_count, 3)
        # Should sleep twice: once for destination change, once for URL+destination change with different URL
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_url_becomes_empty(self, mock_post, mock_thread):
        """Test update when URL becomes empty."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        # First setup initial state
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response]

        self.manager.update("https://example.com", "screenshare")

        # Now set URL to empty
        self.manager.update("", "screenshare")

        self.assertEqual(self.manager.url, "")
        self.stop_bot_output_media_stream_callback.assert_called_once()
        # last_non_empty_url should remain as the last non-empty value
        self.assertEqual(self.manager.last_non_empty_url, "https://example.com")

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_no_change(self, mock_post, mock_thread):
        """Test update when nothing has changed."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        # First setup initial state
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response]

        self.manager.update("https://example.com", "screenshare")

        # Reset mocks
        self.play_bot_output_media_stream_callback.reset_mock()
        self.stop_bot_output_media_stream_callback.reset_mock()

        # Call with same values
        self.manager.update("https://example.com", "screenshare")

        # Should not make any callbacks
        self.play_bot_output_media_stream_callback.assert_not_called()
        self.stop_bot_output_media_stream_callback.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_start_or_update_webrtc_connection_first_time(self, mock_post):
        """Test starting WebRTC connection for the first time."""
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response]

        self.manager.start_or_update_webrtc_connection("https://example.com")

        self.get_peer_connection_offer_callback.assert_called_once()
        self.start_peer_connection_callback.assert_called_once_with({"answer": "test_answer"})
        self.assertTrue(self.manager.webrtc_connection_started)

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_start_or_update_webrtc_connection_with_error(self, mock_post):
        """Test starting WebRTC connection when there's an error in offer."""
        self.get_peer_connection_offer_callback.return_value = {"error": "test_error"}

        self.manager.start_or_update_webrtc_connection("https://example.com")

        self.get_peer_connection_offer_callback.assert_called_once()
        self.start_peer_connection_callback.assert_not_called()
        self.assertFalse(self.manager.webrtc_connection_started)
        mock_post.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_start_or_update_webrtc_connection_failed_start(self, mock_post):
        """Test starting WebRTC connection when start_streaming fails."""
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 500
        mock_post.side_effect = [mock_offer_response, mock_start_response]

        self.manager.start_or_update_webrtc_connection("https://example.com")

        self.get_peer_connection_offer_callback.assert_called_once()
        self.start_peer_connection_callback.assert_called_once()
        self.assertFalse(self.manager.webrtc_connection_started)

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_webrtc_connection(self, mock_post):
        """Test updating an existing WebRTC connection."""
        mock_update_response = Mock()
        mock_update_response.status_code = 200
        mock_post.return_value = mock_update_response

        self.manager.update_webrtc_connection("https://newurl.com")

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("start_streaming", call_args[0][0])
        self.assertEqual(call_args[1]["json"]["url"], "https://newurl.com")

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_webrtc_connection_failed(self, mock_post):
        """Test updating WebRTC connection when it fails."""
        mock_update_response = Mock()
        mock_update_response.status_code = 500
        mock_post.return_value = mock_update_response

        self.manager.update_webrtc_connection("https://newurl.com")

        mock_post.assert_called_once()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_cleanup(self, mock_post):
        """Test cleanup method."""
        mock_shutdown_response = Mock()
        mock_shutdown_response.json.return_value = {"status": "shutdown"}
        mock_post.return_value = mock_shutdown_response

        self.manager.cleanup()

        self.assertTrue(self.manager.cleaned_up)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("shutdown", call_args[0][0])

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_cleanup_with_exception(self, mock_post):
        """Test cleanup method when shutdown request raises an exception."""
        mock_post.side_effect = Exception("Network error")

        # Should not raise exception
        self.manager.cleanup()

        self.assertTrue(self.manager.cleaned_up)
        mock_post.assert_called_once()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_send_webpage_streamer_shutdown_request(self, mock_post):
        """Test sending shutdown request."""
        mock_shutdown_response = Mock()
        mock_shutdown_response.json.return_value = {"status": "shutdown"}
        mock_post.return_value = mock_shutdown_response

        self.manager.send_webpage_streamer_shutdown_request()

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("shutdown", call_args[0][0])

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_send_webpage_streamer_shutdown_request_with_exception(self, mock_post):
        """Test sending shutdown request when it raises an exception."""
        mock_post.side_effect = Exception("Network error")

        # Should not raise exception
        self.manager.send_webpage_streamer_shutdown_request()

        mock_post.assert_called_once()

    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_send_webpage_streamer_keepalive_periodically(self, mock_post, mock_sleep):
        """Test keepalive task sends requests periodically and notifies when started."""
        mock_keepalive_response = Mock()
        mock_keepalive_response.status_code = 200
        mock_post.return_value = mock_keepalive_response

        # Make sleep raise exception after 2 calls to exit the loop
        call_count = [0]

        def sleep_side_effect(seconds):
            call_count[0] += 1
            if call_count[0] >= 2:
                self.manager.cleaned_up = True

        mock_sleep.side_effect = sleep_side_effect

        self.manager.send_webpage_streamer_keepalive_periodically()

        # Should have called sleep twice (10 seconds initially) and sent one keepalive request
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertEqual(mock_sleep.call_args_list[0], call(10))  # First call with 10 seconds
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("keepalive", call_args[0][0])
        # Should have marked streamer as started and called the callback
        self.assertTrue(self.manager.webpage_streamer_has_started)
        self.on_message_that_webpage_streamer_connection_can_start_callback.assert_called_once()

    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_send_webpage_streamer_keepalive_with_exception(self, mock_post, mock_sleep):
        """Test keepalive task continues even when request fails."""
        mock_post.side_effect = Exception("Network error")

        # Make sleep exit after 2 calls
        call_count = [0]

        def sleep_side_effect(seconds):
            call_count[0] += 1
            if call_count[0] >= 2:
                self.manager.cleaned_up = True

        mock_sleep.side_effect = sleep_side_effect

        # Should not raise exception
        self.manager.send_webpage_streamer_keepalive_periodically()

        self.assertEqual(mock_sleep.call_count, 2)
        mock_post.assert_called_once()
        # Callback should not be called since keepalive failed
        self.on_message_that_webpage_streamer_connection_can_start_callback.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_keepalive_stops_when_cleaned_up(self, mock_post, mock_sleep):
        """Test keepalive task stops when manager is cleaned up."""
        mock_keepalive_response = Mock()
        mock_keepalive_response.status_code = 200
        mock_post.return_value = mock_keepalive_response

        # Set cleaned_up after first sleep
        def sleep_side_effect(seconds):
            self.manager.cleaned_up = True

        mock_sleep.side_effect = sleep_side_effect

        self.manager.send_webpage_streamer_keepalive_periodically()

        # Should have called sleep once with 10 seconds (not started yet) and not sent any keepalive (exited before check)
        mock_sleep.assert_called_once_with(10)
        mock_post.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_only_url_changed_same_destination(self, mock_post, mock_sleep, mock_thread):
        """Test update when only URL changed but destination stayed the same."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        # First setup initial state
        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_update_response = Mock()
        mock_update_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response, mock_update_response]

        self.manager.update("https://example.com", "screenshare")

        # Reset mock
        self.play_bot_output_media_stream_callback.reset_mock()

        # Change only URL, keep same destination
        self.manager.update("https://newurl.com", "screenshare")

        self.assertEqual(self.manager.url, "https://newurl.com")
        self.assertEqual(self.manager.output_destination, "screenshare")
        # Should not call play_bot_output_media_stream_callback because only_change_was_url is True
        self.play_bot_output_media_stream_callback.assert_not_called()
        # Should not sleep
        mock_sleep.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.threading.Thread")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_tracks_last_non_empty_url(self, mock_post, mock_thread):
        """Test that last_non_empty_url is properly tracked."""
        # Mock the thread to prevent keepalive from actually running
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        # Set streamer as started
        self.manager.webpage_streamer_has_started = True

        mock_offer_response = Mock()
        mock_offer_response.json.return_value = {"answer": "test_answer"}
        mock_start_response = Mock()
        mock_start_response.status_code = 200
        mock_update_response = Mock()
        mock_update_response.status_code = 200
        mock_post.side_effect = [mock_offer_response, mock_start_response, mock_update_response]

        # Set first URL
        self.manager.update("https://first.com", "screenshare")
        self.assertEqual(self.manager.last_non_empty_url, "https://first.com")

        # Change to second URL
        self.manager.update("https://second.com", "screenshare")
        self.assertEqual(self.manager.last_non_empty_url, "https://second.com")

        # Set empty URL
        self.manager.update("", "screenshare")
        # last_non_empty_url should still be the second URL
        self.assertEqual(self.manager.last_non_empty_url, "https://second.com")

    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_keepalive_changes_sleep_duration_after_started(self, mock_post, mock_sleep):
        """Test keepalive task sleeps for 10s initially and 60s after started."""
        mock_keepalive_response = Mock()
        mock_keepalive_response.status_code = 200
        mock_post.return_value = mock_keepalive_response

        call_count = [0]

        def sleep_side_effect(seconds):
            call_count[0] += 1
            if call_count[0] >= 3:
                self.manager.cleaned_up = True

        mock_sleep.side_effect = sleep_side_effect

        self.manager.send_webpage_streamer_keepalive_periodically()

        # Should have called sleep 3 times: 10s (not started), 60s (after started), 60s (after started)
        self.assertEqual(mock_sleep.call_count, 3)
        self.assertEqual(mock_sleep.call_args_list[0], call(10))  # First call with 10 seconds
        self.assertEqual(mock_sleep.call_args_list[1], call(60))  # Second call with 60 seconds
        self.assertEqual(mock_sleep.call_args_list[2], call(60))  # Third call with 60 seconds
        # Should have sent two keepalive requests
        self.assertEqual(mock_post.call_count, 2)
