import os
import threading
from unittest.mock import MagicMock, patch

from django.test import TestCase

from bots.bot_controller.webpage_streamer_manager import WebpageStreamerManager


class TestWebpageStreamerManagerInit(TestCase):
    """Tests for WebpageStreamerManager initialization."""

    def test_init_sets_callbacks_and_defaults(self):
        """Test that __init__ properly sets all callbacks and default values."""
        is_ready_callback = MagicMock()
        get_offer_callback = MagicMock()
        start_pc_callback = MagicMock()
        play_media_callback = MagicMock()
        stop_media_callback = MagicMock()
        on_can_start_callback = MagicMock()

        manager = WebpageStreamerManager(
            is_bot_ready_for_webpage_streamer_callback=is_ready_callback,
            get_peer_connection_offer_callback=get_offer_callback,
            start_peer_connection_callback=start_pc_callback,
            play_bot_output_media_stream_callback=play_media_callback,
            stop_bot_output_media_stream_callback=stop_media_callback,
            on_message_that_webpage_streamer_connection_can_start_callback=on_can_start_callback,
            webpage_streamer_service_hostname="test-hostname",
        )

        self.assertEqual(manager.urls, {"webcam": None, "screenshare": None})
        self.assertEqual(manager.get_peer_connection_offer_callback, get_offer_callback)
        self.assertEqual(manager.start_peer_connection_callback, start_pc_callback)
        self.assertFalse(manager.cleaned_up)
        self.assertEqual(manager.webpage_streamer_service_hostname, "test-hostname")
        self.assertEqual(manager.is_bot_ready_for_webpage_streamer_callback, is_ready_callback)
        self.assertEqual(manager.play_bot_output_media_stream_callback, play_media_callback)
        self.assertEqual(manager.stop_bot_output_media_stream_callback, stop_media_callback)
        self.assertEqual(manager.on_message_that_webpage_streamer_connection_can_start_callback, on_can_start_callback)
        self.assertEqual(manager.webrtc_connection_started, {"webcam": False, "screenshare": False})
        self.assertIsNone(manager.keepalive_task)
        self.assertFalse(manager.webpage_streamer_connection_can_start)


class TestWebpageStreamerManagerStreamingServiceHostname(TestCase):
    """Tests for streaming_service_hostname method."""

    def _create_manager(self):
        """Helper to create a manager with mock callbacks."""
        return WebpageStreamerManager(
            is_bot_ready_for_webpage_streamer_callback=MagicMock(),
            get_peer_connection_offer_callback=MagicMock(),
            start_peer_connection_callback=MagicMock(),
            play_bot_output_media_stream_callback=MagicMock(),
            stop_bot_output_media_stream_callback=MagicMock(),
            on_message_that_webpage_streamer_connection_can_start_callback=MagicMock(),
            webpage_streamer_service_hostname="k8s-service-hostname",
        )

    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "kubernetes"})
    def test_returns_service_hostname_in_kubernetes(self):
        """Test that streaming_service_hostname returns the service hostname in k8s."""
        manager = self._create_manager()
        result = manager.streaming_service_hostname()
        self.assertEqual(result, "k8s-service-hostname")

    @patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "docker"})
    def test_returns_local_hostname_in_docker(self):
        """Test that streaming_service_hostname returns local hostname in docker."""
        manager = self._create_manager()
        result = manager.streaming_service_hostname()
        self.assertEqual(result, "attendee-webpage-streamer-local")

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_local_hostname_when_no_env_var(self):
        """Test that streaming_service_hostname returns local hostname when env var not set."""
        manager = self._create_manager()
        result = manager.streaming_service_hostname()
        self.assertEqual(result, "attendee-webpage-streamer-local")


class TestWebpageStreamerManagerUpdate(TestCase):
    """Tests for the update method."""

    def _create_manager(self):
        """Helper to create a manager with mock callbacks."""
        manager = WebpageStreamerManager(
            is_bot_ready_for_webpage_streamer_callback=MagicMock(),
            get_peer_connection_offer_callback=MagicMock(),
            start_peer_connection_callback=MagicMock(),
            play_bot_output_media_stream_callback=MagicMock(),
            stop_bot_output_media_stream_callback=MagicMock(),
            on_message_that_webpage_streamer_connection_can_start_callback=MagicMock(),
            webpage_streamer_service_hostname="test-hostname",
        )
        return manager

    def test_update_does_nothing_when_connection_cannot_start(self):
        """Test that update does nothing when webpage_streamer_connection_can_start is False."""
        manager = self._create_manager()
        manager.webpage_streamer_connection_can_start = False

        manager.update({"webcam": "http://example.com", "screenshare": None})

        # Should not update any values
        self.assertEqual(manager.urls, {"webcam": None, "screenshare": None})
        manager.play_bot_output_media_stream_callback.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_starts_connection_with_new_url(self, mock_post):
        """Test that update starts a WebRTC connection when url is set."""
        manager = self._create_manager()
        manager.webpage_streamer_connection_can_start = True
        manager.get_peer_connection_offer_callback.return_value = {"sdp": "test-sdp", "type": "offer"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"sdp": "answer-sdp", "type": "answer"}
        mock_post.return_value = mock_response

        manager.update({"webcam": "http://example.com", "screenshare": None})

        self.assertEqual(manager.urls, {"webcam": "http://example.com", "screenshare": None})
        self.assertEqual(manager.webrtc_connection_started, {"webcam": True, "screenshare": False})
        manager.get_peer_connection_offer_callback.assert_called_once_with("webcam")
        manager.start_peer_connection_callback.assert_called_once_with({"sdp": "answer-sdp", "type": "answer"}, "webcam")
        manager.play_bot_output_media_stream_callback.assert_called_once_with("webcam")
        manager.stop_bot_output_media_stream_callback.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_starts_both_output_destinations(self, mock_post):
        """Test that update starts a separate WebRTC connection for webcam and screenshare."""
        manager = self._create_manager()
        manager.webpage_streamer_connection_can_start = True
        manager.get_peer_connection_offer_callback.return_value = {"sdp": "test-sdp", "type": "offer"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"sdp": "answer-sdp", "type": "answer"}
        mock_post.return_value = mock_response

        manager.update({"webcam": "https://agent.example.com", "screenshare": "https://slides.example.com"})

        self.assertEqual(manager.urls, {"webcam": "https://agent.example.com", "screenshare": "https://slides.example.com"})
        self.assertEqual(manager.webrtc_connection_started, {"webcam": True, "screenshare": True})
        self.assertEqual([c.args for c in manager.get_peer_connection_offer_callback.call_args_list], [("webcam",), ("screenshare",)])
        self.assertEqual([c.args for c in manager.play_bot_output_media_stream_callback.call_args_list], [("webcam",), ("screenshare",)])

        start_streaming_bodies = [c.kwargs["json"] for c in mock_post.call_args_list if c.args[0].endswith("/start_streaming")]
        self.assertEqual(
            start_streaming_bodies,
            [
                {"url": "https://agent.example.com", "output_destination": "webcam"},
                {"url": "https://slides.example.com", "output_destination": "screenshare"},
            ],
        )
        offer_bodies = [c.kwargs["json"] for c in mock_post.call_args_list if c.args[0].endswith("/offer")]
        self.assertEqual([body["output_destination"] for body in offer_bodies], ["webcam", "screenshare"])

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_stops_stream_when_url_becomes_empty(self, mock_post):
        """Test that update stops only that destination's media stream when its url becomes empty."""
        manager = self._create_manager()
        manager.webpage_streamer_connection_can_start = True
        manager.urls = {"webcam": "http://example.com", "screenshare": "http://slides.example.com"}

        manager.update({"webcam": "", "screenshare": "http://slides.example.com"})

        manager.stop_bot_output_media_stream_callback.assert_called_once_with("webcam")
        manager.play_bot_output_media_stream_callback.assert_not_called()
        mock_post.assert_not_called()
        self.assertEqual(manager.urls, {"webcam": "", "screenshare": "http://slides.example.com"})

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_does_not_stop_stream_that_was_never_started(self, mock_post):
        """Test that an empty url for a destination that was never started is a no-op."""
        manager = self._create_manager()
        manager.webpage_streamer_connection_can_start = True

        manager.update({"webcam": "", "screenshare": None})

        manager.stop_bot_output_media_stream_callback.assert_not_called()
        manager.play_bot_output_media_stream_callback.assert_not_called()
        mock_post.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_only_changes_url_without_replaying(self, mock_post):
        """Test that changing only the URL doesn't stop/replay the stream."""
        manager = self._create_manager()
        manager.webpage_streamer_connection_can_start = True
        manager.webrtc_connection_started = {"webcam": True, "screenshare": False}
        manager.urls = {"webcam": "http://example.com", "screenshare": None}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        manager.update({"webcam": "http://example2.com", "screenshare": None})

        # Should not call play_bot_output_media_stream_callback since the stream is already playing
        manager.play_bot_output_media_stream_callback.assert_not_called()
        manager.stop_bot_output_media_stream_callback.assert_not_called()
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["json"], {"url": "http://example2.com", "output_destination": "webcam"})
        self.assertEqual(manager.urls["webcam"], "http://example2.com")

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_restarts_playback_after_url_turned_off_and_on(self, mock_post):
        """Test that re-enabling a destination reuses its WebRTC connection and plays the stream again."""
        manager = self._create_manager()
        manager.webpage_streamer_connection_can_start = True
        manager.webrtc_connection_started = {"webcam": False, "screenshare": True}
        manager.urls = {"webcam": None, "screenshare": ""}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        manager.update({"webcam": None, "screenshare": "http://slides2.example.com"})

        manager.get_peer_connection_offer_callback.assert_not_called()
        manager.play_bot_output_media_stream_callback.assert_called_once_with("screenshare")
        mock_post.assert_called_once()
        self.assertIn("start_streaming", mock_post.call_args.args[0])


class TestWebpageStreamerManagerStartOrUpdateWebrtcConnection(TestCase):
    """Tests for start_or_update_webrtc_connection method."""

    def _create_manager(self):
        """Helper to create a manager with mock callbacks."""
        return WebpageStreamerManager(
            is_bot_ready_for_webpage_streamer_callback=MagicMock(),
            get_peer_connection_offer_callback=MagicMock(),
            start_peer_connection_callback=MagicMock(),
            play_bot_output_media_stream_callback=MagicMock(),
            stop_bot_output_media_stream_callback=MagicMock(),
            on_message_that_webpage_streamer_connection_can_start_callback=MagicMock(),
            webpage_streamer_service_hostname="test-hostname",
        )

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_start_webrtc_connection_success(self, mock_post):
        """Test successful WebRTC connection start."""
        manager = self._create_manager()
        manager.get_peer_connection_offer_callback.return_value = {"sdp": "offer-sdp", "type": "offer"}

        mock_offer_response = MagicMock()
        mock_offer_response.json.return_value = {"sdp": "answer-sdp", "type": "answer"}

        mock_start_response = MagicMock()
        mock_start_response.status_code = 200

        mock_post.side_effect = [mock_offer_response, mock_start_response]

        manager.start_or_update_webrtc_connection("screenshare", "http://example.com")

        self.assertEqual(manager.webrtc_connection_started, {"webcam": False, "screenshare": True})
        manager.start_peer_connection_callback.assert_called_once_with({"sdp": "answer-sdp", "type": "answer"}, "screenshare")
        self.assertEqual(mock_post.call_count, 2)

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_start_webrtc_connection_fails_on_offer_error(self, mock_post):
        """Test that WebRTC connection doesn't start when offer has an error."""
        manager = self._create_manager()
        manager.get_peer_connection_offer_callback.return_value = {"error": "Failed to create offer"}

        manager.start_or_update_webrtc_connection("webcam", "http://example.com")

        self.assertFalse(manager.webrtc_connection_started["webcam"])
        mock_post.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_start_webrtc_connection_fails_on_streaming_error(self, mock_post):
        """Test that webrtc_connection_started stays False when streaming fails."""
        manager = self._create_manager()
        manager.get_peer_connection_offer_callback.return_value = {"sdp": "offer-sdp", "type": "offer"}

        mock_offer_response = MagicMock()
        mock_offer_response.json.return_value = {"sdp": "answer-sdp", "type": "answer"}

        mock_start_response = MagicMock()
        mock_start_response.status_code = 500

        mock_post.side_effect = [mock_offer_response, mock_start_response]

        manager.start_or_update_webrtc_connection("webcam", "http://example.com")

        self.assertFalse(manager.webrtc_connection_started["webcam"])

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_update_webrtc_connection_when_already_started(self, mock_post):
        """Test that update is called when connection is already started."""
        manager = self._create_manager()
        manager.webrtc_connection_started["webcam"] = True

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        manager.start_or_update_webrtc_connection("webcam", "http://newurl.com")

        # Should only call start_streaming, not offer
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("start_streaming", call_args[0][0])


class TestWebpageStreamerManagerCleanup(TestCase):
    """Tests for cleanup method."""

    def _create_manager(self):
        """Helper to create a manager with mock callbacks."""
        return WebpageStreamerManager(
            is_bot_ready_for_webpage_streamer_callback=MagicMock(),
            get_peer_connection_offer_callback=MagicMock(),
            start_peer_connection_callback=MagicMock(),
            play_bot_output_media_stream_callback=MagicMock(),
            stop_bot_output_media_stream_callback=MagicMock(),
            on_message_that_webpage_streamer_connection_can_start_callback=MagicMock(),
            webpage_streamer_service_hostname="test-hostname",
        )

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_cleanup_sends_shutdown_request(self, mock_post):
        """Test that cleanup sends a shutdown request."""
        manager = self._create_manager()

        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok"}
        mock_post.return_value = mock_response

        manager.cleanup()

        self.assertTrue(manager.cleaned_up)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("shutdown", call_args[0][0])

    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_cleanup_handles_shutdown_exception(self, mock_post):
        """Test that cleanup handles exceptions gracefully."""
        manager = self._create_manager()

        mock_post.side_effect = Exception("Connection refused")

        # Should not raise exception
        manager.cleanup()

        self.assertTrue(manager.cleaned_up)


class TestWebpageStreamerManagerInitMethod(TestCase):
    """Tests for init() method that starts keepalive thread."""

    def _create_manager(self):
        """Helper to create a manager with mock callbacks."""
        return WebpageStreamerManager(
            is_bot_ready_for_webpage_streamer_callback=MagicMock(),
            get_peer_connection_offer_callback=MagicMock(),
            start_peer_connection_callback=MagicMock(),
            play_bot_output_media_stream_callback=MagicMock(),
            stop_bot_output_media_stream_callback=MagicMock(),
            on_message_that_webpage_streamer_connection_can_start_callback=MagicMock(),
            webpage_streamer_service_hostname="test-hostname",
        )

    def test_init_starts_keepalive_thread(self):
        """Test that init() starts a keepalive thread."""
        manager = self._create_manager()
        manager.cleaned_up = True  # Prevent the thread from running forever

        manager.init()

        self.assertIsNotNone(manager.keepalive_task)
        self.assertIsInstance(manager.keepalive_task, threading.Thread)
        self.assertTrue(manager.keepalive_task.daemon)

    def test_init_does_not_start_second_thread(self):
        """Test that init() doesn't start a second thread if already running."""
        manager = self._create_manager()
        manager.cleaned_up = True  # Prevent the thread from running forever

        manager.init()
        first_task = manager.keepalive_task

        manager.init()

        self.assertEqual(manager.keepalive_task, first_task)


class TestWebpageStreamerManagerKeepalive(TestCase):
    """Tests for keepalive functionality."""

    def _create_manager(self):
        """Helper to create a manager with mock callbacks."""
        return WebpageStreamerManager(
            is_bot_ready_for_webpage_streamer_callback=MagicMock(return_value=True),
            get_peer_connection_offer_callback=MagicMock(),
            start_peer_connection_callback=MagicMock(),
            play_bot_output_media_stream_callback=MagicMock(),
            stop_bot_output_media_stream_callback=MagicMock(),
            on_message_that_webpage_streamer_connection_can_start_callback=MagicMock(),
            webpage_streamer_service_hostname="test-hostname",
        )

    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_keepalive_sets_connection_can_start_on_success(self, mock_post, mock_sleep):
        """Test that keepalive sets webpage_streamer_connection_can_start when service responds."""
        manager = self._create_manager()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Make sleep set cleaned_up on the second call to break the loop after first iteration completes
        sleep_call_count = [0]

        def sleep_side_effect(duration):
            sleep_call_count[0] += 1
            if sleep_call_count[0] >= 2:
                manager.cleaned_up = True

        mock_sleep.side_effect = sleep_side_effect

        manager.send_webpage_streamer_keepalive_periodically()

        self.assertTrue(manager.webpage_streamer_connection_can_start)
        manager.on_message_that_webpage_streamer_connection_can_start_callback.assert_called_once()

    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_keepalive_does_not_notify_if_bot_not_ready(self, mock_post, mock_sleep):
        """Test that keepalive doesn't notify when bot is not ready."""
        manager = self._create_manager()
        manager.is_bot_ready_for_webpage_streamer_callback.return_value = False

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Make sleep set cleaned_up on the second call to break the loop after first iteration completes
        sleep_call_count = [0]

        def sleep_side_effect(duration):
            sleep_call_count[0] += 1
            if sleep_call_count[0] >= 2:
                manager.cleaned_up = True

        mock_sleep.side_effect = sleep_side_effect

        manager.send_webpage_streamer_keepalive_periodically()

        self.assertFalse(manager.webpage_streamer_connection_can_start)
        manager.on_message_that_webpage_streamer_connection_can_start_callback.assert_not_called()

    @patch("bots.bot_controller.webpage_streamer_manager.time.sleep")
    @patch("bots.bot_controller.webpage_streamer_manager.requests.post")
    def test_keepalive_handles_request_exception(self, mock_post, mock_sleep):
        """Test that keepalive continues after request exception."""
        manager = self._create_manager()

        call_count = [0]

        def post_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Connection refused")
            mock_response = MagicMock()
            mock_response.status_code = 200
            return mock_response

        mock_post.side_effect = post_side_effect

        # Make sleep set cleaned_up to break the loop after second iteration
        sleep_call_count = [0]

        times_to_try = 3

        def sleep_side_effect(duration):
            sleep_call_count[0] += 1
            if sleep_call_count[0] > times_to_try:
                manager.cleaned_up = True

        mock_sleep.side_effect = sleep_side_effect

        # Should not raise exception
        manager.send_webpage_streamer_keepalive_periodically()

        # Should have tried twice
        self.assertEqual(mock_post.call_count, times_to_try)
