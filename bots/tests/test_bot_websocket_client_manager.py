import unittest
from unittest.mock import Mock, call, patch

from bots.bot_controller.bot_websocket_client import BotWebsocketClient
from bots.bot_controller.bot_websocket_client_manager import BotWebsocketClientManager

MIXED_URL = "wss://mixed.example.com/audio"
PER_PARTICIPANT_AUDIO_URL = "wss://per-participant.example.com/audio"
PER_PARTICIPANT_VIDEO_URL = "wss://per-participant.example.com/video"
SHARED_URL = "wss://shared.example.com/stream"


@patch("bots.bot_controller.bot_websocket_client_manager.BotWebsocketClient")
class TestBotWebsocketClientManager(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.mock_callback = Mock()

    def tearDown(self):
        """Clean up after each test method."""
        self.mock_callback.reset_mock()

    # --------------------------------------------------------------------- #
    #  Initialization tests                                                 #
    # --------------------------------------------------------------------- #

    def test_initialization_all_three_urls(self, MockClient):
        """Test that three distinct URLs create three separate clients."""
        mock_instances = [Mock(spec=BotWebsocketClient), Mock(spec=BotWebsocketClient), Mock(spec=BotWebsocketClient)]
        MockClient.side_effect = mock_instances

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(MockClient.call_count, 3)
        self.assertEqual(len(mgr._clients), 3)
        self.assertIsNotNone(mgr._mixed_audio_client)
        self.assertIsNotNone(mgr._per_participant_audio_client)
        self.assertIsNotNone(mgr._per_participant_video_client)
        self.assertIsNot(mgr._mixed_audio_client, mgr._per_participant_audio_client)
        self.assertIsNot(mgr._mixed_audio_client, mgr._per_participant_video_client)
        self.assertIsNot(mgr._per_participant_audio_client, mgr._per_participant_video_client)

    def test_initialization_mixed_and_per_participant_audio(self, MockClient):
        """Test that two distinct audio URLs create two separate clients."""
        mock_instances = [Mock(spec=BotWebsocketClient), Mock(spec=BotWebsocketClient)]
        MockClient.side_effect = mock_instances

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(MockClient.call_count, 2)
        self.assertEqual(len(mgr._clients), 2)
        self.assertIsNotNone(mgr._mixed_audio_client)
        self.assertIsNotNone(mgr._per_participant_audio_client)
        self.assertIsNone(mgr._per_participant_video_client)

    def test_initialization_shared_url_creates_single_client(self, MockClient):
        """Test that identical URLs share a single underlying client."""
        mock_instance = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_instance

        mgr = BotWebsocketClientManager(
            mixed_audio_url=SHARED_URL,
            per_participant_audio_url=SHARED_URL,
            per_participant_video_url=SHARED_URL,
            on_message_callback=self.mock_callback,
        )

        MockClient.assert_called_once()
        self.assertEqual(len(mgr._clients), 1)
        self.assertIs(mgr._mixed_audio_client, mgr._per_participant_audio_client)
        self.assertIs(mgr._mixed_audio_client, mgr._per_participant_video_client)

    def test_initialization_per_participant_audio_and_video_share_url(self, MockClient):
        """Test that per-participant audio and video sharing a URL creates one client for them."""
        pp_client = Mock(spec=BotWebsocketClient)
        mixed_client = Mock(spec=BotWebsocketClient)
        MockClient.side_effect = [mixed_client, pp_client]

        pp_shared = "wss://per-participant.example.com/shared"
        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=pp_shared,
            per_participant_video_url=pp_shared,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(MockClient.call_count, 2)
        self.assertEqual(len(mgr._clients), 2)
        self.assertIs(mgr._per_participant_audio_client, mgr._per_participant_video_client)
        self.assertIsNot(mgr._mixed_audio_client, mgr._per_participant_audio_client)

    def test_initialization_only_mixed_url(self, MockClient):
        """Test that only a mixed audio client is created when other URLs are None."""
        mock_instance = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_instance

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        MockClient.assert_called_once()
        self.assertIsNotNone(mgr._mixed_audio_client)
        self.assertIsNone(mgr._per_participant_audio_client)
        self.assertIsNone(mgr._per_participant_video_client)
        self.assertEqual(len(mgr._clients), 1)

    def test_initialization_only_per_participant_audio_url(self, MockClient):
        """Test that only a per-participant audio client is created when other URLs are None."""
        mock_instance = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_instance

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        MockClient.assert_called_once()
        self.assertIsNone(mgr._mixed_audio_client)
        self.assertIsNotNone(mgr._per_participant_audio_client)
        self.assertIsNone(mgr._per_participant_video_client)
        self.assertEqual(len(mgr._clients), 1)

    def test_initialization_only_per_participant_video_url(self, MockClient):
        """Test that only a per-participant video client is created when other URLs are None."""
        mock_instance = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_instance

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        MockClient.assert_called_once()
        self.assertIsNone(mgr._mixed_audio_client)
        self.assertIsNone(mgr._per_participant_audio_client)
        self.assertIsNotNone(mgr._per_participant_video_client)
        self.assertEqual(len(mgr._clients), 1)

    def test_initialization_no_urls(self, MockClient):
        """Test that no clients are created when all URLs are None."""
        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        MockClient.assert_not_called()
        self.assertIsNone(mgr._mixed_audio_client)
        self.assertIsNone(mgr._per_participant_audio_client)
        self.assertIsNone(mgr._per_participant_video_client)
        self.assertEqual(len(mgr._clients), 0)
        self.assertEqual(len(mgr._url_to_purposes), 0)

    def test_initialization_empty_string_urls_treated_as_absent(self, MockClient):
        """Test that empty string URLs are treated identically to None."""
        mgr = BotWebsocketClientManager(
            mixed_audio_url="",
            per_participant_audio_url="",
            per_participant_video_url="",
            on_message_callback=self.mock_callback,
        )

        MockClient.assert_not_called()
        self.assertIsNone(mgr._mixed_audio_client)
        self.assertIsNone(mgr._per_participant_audio_client)
        self.assertIsNone(mgr._per_participant_video_client)
        self.assertEqual(len(mgr._clients), 0)

    def test_callback_forwarded_to_client(self, MockClient):
        """Test that the on_message_callback is correctly forwarded to BotWebsocketClient."""
        cb = Mock()
        BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=cb,
        )

        MockClient.assert_called_once_with(url=MIXED_URL, on_message_callback=cb)

    def test_callback_forwarded_to_all_clients(self, MockClient):
        """Test that the same callback is forwarded when three distinct clients are created."""
        cb = Mock()
        BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=cb,
        )

        self.assertEqual(MockClient.call_count, 3)
        for c in MockClient.call_args_list:
            self.assertEqual(c.kwargs["on_message_callback"], cb)

    # --------------------------------------------------------------------- #
    #  Purpose tracking tests                                               #
    # --------------------------------------------------------------------- #

    def test_purpose_tracking_separate_urls(self, MockClient):
        """Test that each URL is tagged with its single purpose."""
        MockClient.side_effect = lambda **kw: Mock(spec=BotWebsocketClient, websocket_url=kw["url"])

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        self.assertIn(MIXED_URL, mgr._url_to_purposes)
        self.assertIn(PER_PARTICIPANT_AUDIO_URL, mgr._url_to_purposes)
        self.assertIn(PER_PARTICIPANT_VIDEO_URL, mgr._url_to_purposes)
        self.assertEqual(mgr._url_to_purposes[MIXED_URL], ["mixed_audio"])
        self.assertEqual(mgr._url_to_purposes[PER_PARTICIPANT_AUDIO_URL], ["per_participant_audio"])
        self.assertEqual(mgr._url_to_purposes[PER_PARTICIPANT_VIDEO_URL], ["per_participant_video"])

    def test_purpose_tracking_all_shared_url(self, MockClient):
        """Test that a fully shared URL is tagged with all three purposes."""
        MockClient.return_value = Mock(spec=BotWebsocketClient, websocket_url=SHARED_URL)

        mgr = BotWebsocketClientManager(
            mixed_audio_url=SHARED_URL,
            per_participant_audio_url=SHARED_URL,
            per_participant_video_url=SHARED_URL,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(len(mgr._url_to_purposes), 1)
        self.assertEqual(
            mgr._url_to_purposes[SHARED_URL],
            ["mixed_audio", "per_participant_audio", "per_participant_video"],
        )

    def test_purpose_tracking_per_participant_shared_url(self, MockClient):
        """Test purpose tracking when per-participant audio and video share a URL."""
        pp_shared = "wss://pp.example.com/shared"
        MockClient.side_effect = lambda **kw: Mock(spec=BotWebsocketClient, websocket_url=kw["url"])

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=pp_shared,
            per_participant_video_url=pp_shared,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(len(mgr._url_to_purposes), 2)
        self.assertEqual(mgr._url_to_purposes[MIXED_URL], ["mixed_audio"])
        self.assertEqual(mgr._url_to_purposes[pp_shared], ["per_participant_audio", "per_participant_video"])

    def test_purpose_tracking_single_url(self, MockClient):
        """Test that a single URL only has one purpose entry."""
        MockClient.return_value = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(len(mgr._url_to_purposes), 1)
        self.assertEqual(mgr._url_to_purposes[MIXED_URL], ["mixed_audio"])

    def test_no_purpose_entries_when_no_urls(self, MockClient):
        """Test that _url_to_purposes is empty when no URLs are provided."""
        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(mgr._url_to_purposes, {})

    # --------------------------------------------------------------------- #
    #  _ensure_started tests                                                #
    # --------------------------------------------------------------------- #

    def test_ensure_started_starts_unstarted_client(self, MockClient):
        """Test that _ensure_started calls start() on a client that hasn't been started."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)
        mock_client.started.return_value = False
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr._ensure_started(mock_client)

        mock_client.started.assert_called_once()
        mock_client.start.assert_called_once()

    def test_ensure_started_skips_already_started_client(self, MockClient):
        """Test that _ensure_started does not call start() on an already-started client."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)
        mock_client.started.return_value = True
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr._ensure_started(mock_client)

        mock_client.started.assert_called_once()
        mock_client.start.assert_not_called()

    def test_ensure_started_logs_purpose(self, MockClient):
        """Test that _ensure_started logs the purposes associated with the client URL."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=SHARED_URL)
        mock_client.started.return_value = False
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=SHARED_URL,
            per_participant_audio_url=SHARED_URL,
            per_participant_video_url=SHARED_URL,
            on_message_callback=self.mock_callback,
        )

        with patch("bots.bot_controller.bot_websocket_client_manager.logger") as mock_logger:
            mgr._ensure_started(mock_client)

            mock_logger.info.assert_called_once()
            log_message = mock_logger.info.call_args[0][1]
            self.assertIn("mixed_audio", log_message)
            self.assertIn("per_participant_audio", log_message)
            self.assertIn("per_participant_video", log_message)

    # --------------------------------------------------------------------- #
    #  send_mixed_audio tests                                               #
    # --------------------------------------------------------------------- #

    def test_send_mixed_audio_starts_and_sends(self, MockClient):
        """Test that send_mixed_audio starts an unstarted client and sends the payload."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)
        mock_client.started.return_value = False
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        payload = {"trigger": "audio", "data": "abc"}
        mgr.send_mixed_audio(payload)

        mock_client.start.assert_called_once()
        mock_client.send_async.assert_called_once_with(payload)

    def test_send_mixed_audio_does_not_restart_already_started(self, MockClient):
        """Test that send_mixed_audio does not call start() if the client is already started."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)
        mock_client.started.return_value = True
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.send_mixed_audio({"data": "1"})
        mgr.send_mixed_audio({"data": "2"})

        mock_client.start.assert_not_called()
        self.assertEqual(mock_client.send_async.call_count, 2)

    def test_send_mixed_audio_noop_when_no_client(self, MockClient):
        """Test that send_mixed_audio is a no-op when no mixed audio URL was configured."""
        mock_client = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.send_mixed_audio({"data": "ignored"})

        for client in mgr._clients:
            client.send_async.assert_not_called()
            client.start.assert_not_called()

    def test_send_mixed_audio_multiple_payloads_preserves_order(self, MockClient):
        """Test that multiple payloads are forwarded in order."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)
        mock_client.started.return_value = True
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        payloads = [{"seq": i} for i in range(5)]
        for p in payloads:
            mgr.send_mixed_audio(p)

        expected = [call(p) for p in payloads]
        mock_client.send_async.assert_has_calls(expected)
        self.assertEqual(mock_client.send_async.call_count, 5)

    # --------------------------------------------------------------------- #
    #  send_per_participant_audio tests                                     #
    # --------------------------------------------------------------------- #

    def test_send_per_participant_audio_starts_and_sends(self, MockClient):
        """Test that send_per_participant_audio starts an unstarted client and sends the payload."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_AUDIO_URL)
        mock_client.started.return_value = False
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        payload = {"trigger": "audio", "participant": "p1"}
        mgr.send_per_participant_audio(payload)

        mock_client.start.assert_called_once()
        mock_client.send_async.assert_called_once_with(payload)

    def test_send_per_participant_audio_does_not_restart_already_started(self, MockClient):
        """Test that send_per_participant_audio does not call start() if the client is already started."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_AUDIO_URL)
        mock_client.started.return_value = True
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.send_per_participant_audio({"data": "1"})
        mgr.send_per_participant_audio({"data": "2"})

        mock_client.start.assert_not_called()
        self.assertEqual(mock_client.send_async.call_count, 2)

    def test_send_per_participant_audio_noop_when_no_client(self, MockClient):
        """Test that send_per_participant_audio is a no-op when no per-participant audio URL was configured."""
        mock_client = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.send_per_participant_audio({"data": "ignored"})

        for client in mgr._clients:
            client.send_async.assert_not_called()
            client.start.assert_not_called()

    def test_send_per_participant_audio_multiple_payloads_preserves_order(self, MockClient):
        """Test that multiple payloads are forwarded in order."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_AUDIO_URL)
        mock_client.started.return_value = True
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        payloads = [{"participant": f"p{i}"} for i in range(5)]
        for p in payloads:
            mgr.send_per_participant_audio(p)

        expected = [call(p) for p in payloads]
        mock_client.send_async.assert_has_calls(expected)
        self.assertEqual(mock_client.send_async.call_count, 5)

    # --------------------------------------------------------------------- #
    #  send_per_participant_video tests                                     #
    # --------------------------------------------------------------------- #

    def test_send_per_participant_video_starts_and_sends(self, MockClient):
        """Test that send_per_participant_video starts an unstarted client and sends the payload."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_VIDEO_URL)
        mock_client.started.return_value = False
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        payload = {"trigger": "video", "participant": "p1", "frame": b"..."}
        mgr.send_per_participant_video(payload)

        mock_client.start.assert_called_once()
        mock_client.send_async.assert_called_once_with(payload)

    def test_send_per_participant_video_does_not_restart_already_started(self, MockClient):
        """Test that send_per_participant_video does not call start() if the client is already started."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_VIDEO_URL)
        mock_client.started.return_value = True
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        mgr.send_per_participant_video({"data": "1"})
        mgr.send_per_participant_video({"data": "2"})

        mock_client.start.assert_not_called()
        self.assertEqual(mock_client.send_async.call_count, 2)

    def test_send_per_participant_video_noop_when_no_client(self, MockClient):
        """Test that send_per_participant_video is a no-op when no per-participant video URL was configured."""
        mock_client = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.send_per_participant_video({"data": "ignored"})

        for client in mgr._clients:
            client.send_async.assert_not_called()
            client.start.assert_not_called()

    def test_send_per_participant_video_multiple_payloads_preserves_order(self, MockClient):
        """Test that multiple payloads are forwarded in order."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_VIDEO_URL)
        mock_client.started.return_value = True
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        payloads = [{"participant": f"p{i}", "frame": i} for i in range(5)]
        for p in payloads:
            mgr.send_per_participant_video(p)

        expected = [call(p) for p in payloads]
        mock_client.send_async.assert_has_calls(expected)
        self.assertEqual(mock_client.send_async.call_count, 5)

    # --------------------------------------------------------------------- #
    #  Shared-client send tests                                             #
    # --------------------------------------------------------------------- #

    def test_shared_client_receives_all_send_types(self, MockClient):
        """Test that a shared client receives payloads from all three send methods."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=SHARED_URL)
        mock_client.started.return_value = False
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=SHARED_URL,
            per_participant_audio_url=SHARED_URL,
            per_participant_video_url=SHARED_URL,
            on_message_callback=self.mock_callback,
        )

        mixed_payload = {"trigger": "mixed"}
        pp_audio_payload = {"trigger": "per_participant_audio"}
        pp_video_payload = {"trigger": "per_participant_video"}

        mgr.send_mixed_audio(mixed_payload)
        mock_client.started.return_value = True
        mgr.send_per_participant_audio(pp_audio_payload)
        mgr.send_per_participant_video(pp_video_payload)

        mock_client.start.assert_called_once()
        mock_client.send_async.assert_has_calls(
            [
                call(mixed_payload),
                call(pp_audio_payload),
                call(pp_video_payload),
            ]
        )
        self.assertEqual(mock_client.send_async.call_count, 3)

    def test_shared_client_started_only_once_across_interleaved_sends(self, MockClient):
        """Test that interleaved sends on a shared client only trigger start() once."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=SHARED_URL)
        started = False

        def fake_started():
            return started

        def fake_start():
            nonlocal started
            started = True

        mock_client.started = fake_started
        mock_client.start = Mock(side_effect=fake_start)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=SHARED_URL,
            per_participant_audio_url=SHARED_URL,
            per_participant_video_url=SHARED_URL,
            on_message_callback=self.mock_callback,
        )

        mgr.send_mixed_audio({"seq": 1})
        mgr.send_per_participant_audio({"seq": 2})
        mgr.send_per_participant_video({"seq": 3})
        mgr.send_mixed_audio({"seq": 4})
        mgr.send_per_participant_video({"seq": 5})

        mock_client.start.assert_called_once()
        self.assertEqual(mock_client.send_async.call_count, 5)

    def test_separate_clients_started_independently(self, MockClient):
        """Test that three distinct clients are each started on their first respective send."""
        mock_mixed = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)
        mock_mixed.started.return_value = False
        mock_pp_audio = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_AUDIO_URL)
        mock_pp_audio.started.return_value = False
        mock_pp_video = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_VIDEO_URL)
        mock_pp_video.started.return_value = False
        MockClient.side_effect = [mock_mixed, mock_pp_audio, mock_pp_video]

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        mgr.send_mixed_audio({"trigger": "mixed"})
        mock_mixed.start.assert_called_once()
        mock_pp_audio.start.assert_not_called()
        mock_pp_video.start.assert_not_called()

        mgr.send_per_participant_audio({"trigger": "pp_audio"})
        mock_pp_audio.start.assert_called_once()
        mock_pp_video.start.assert_not_called()

        mgr.send_per_participant_video({"trigger": "pp_video"})
        mock_pp_video.start.assert_called_once()
        self.assertEqual(mock_mixed.start.call_count, 1)
        self.assertEqual(mock_pp_audio.start.call_count, 1)

    # --------------------------------------------------------------------- #
    #  Cleanup tests                                                        #
    # --------------------------------------------------------------------- #

    def test_cleanup_calls_cleanup_on_all_clients(self, MockClient):
        """Test that cleanup() is called on every underlying client."""
        clients = [Mock(spec=BotWebsocketClient), Mock(spec=BotWebsocketClient), Mock(spec=BotWebsocketClient)]
        MockClient.side_effect = clients

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        mgr.cleanup()

        for client in clients:
            client.cleanup.assert_called_once()

    def test_cleanup_shared_client_called_once(self, MockClient):
        """Test that a shared client's cleanup() is only called once."""
        mock_client = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=SHARED_URL,
            per_participant_audio_url=SHARED_URL,
            per_participant_video_url=SHARED_URL,
            on_message_callback=self.mock_callback,
        )

        mgr.cleanup()

        mock_client.cleanup.assert_called_once()

    def test_cleanup_no_clients(self, MockClient):
        """Test that cleanup() does not raise when there are no clients."""
        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.cleanup()

    def test_cleanup_only_mixed_client(self, MockClient):
        """Test cleanup when only a mixed audio client exists."""
        mock_client = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.cleanup()

        mock_client.cleanup.assert_called_once()

    def test_cleanup_only_per_participant_audio_client(self, MockClient):
        """Test cleanup when only a per-participant audio client exists."""
        mock_client = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.cleanup()

        mock_client.cleanup.assert_called_once()

    def test_cleanup_only_per_participant_video_client(self, MockClient):
        """Test cleanup when only a per-participant video client exists."""
        mock_client = Mock(spec=BotWebsocketClient)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        mgr.cleanup()

        mock_client.cleanup.assert_called_once()

    # --------------------------------------------------------------------- #
    #  Integration-style tests                                              #
    # --------------------------------------------------------------------- #

    def test_full_lifecycle_separate_clients(self, MockClient):
        """Test a full lifecycle: init -> send on all three channels -> cleanup."""
        mock_mixed = Mock(spec=BotWebsocketClient, websocket_url=MIXED_URL)
        mock_mixed.started.return_value = False
        mock_pp_audio = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_AUDIO_URL)
        mock_pp_audio.started.return_value = False
        mock_pp_video = Mock(spec=BotWebsocketClient, websocket_url=PER_PARTICIPANT_VIDEO_URL)
        mock_pp_video.started.return_value = False
        MockClient.side_effect = [mock_mixed, mock_pp_audio, mock_pp_video]

        mgr = BotWebsocketClientManager(
            mixed_audio_url=MIXED_URL,
            per_participant_audio_url=PER_PARTICIPANT_AUDIO_URL,
            per_participant_video_url=PER_PARTICIPANT_VIDEO_URL,
            on_message_callback=self.mock_callback,
        )

        mixed_payload = {"trigger": "mixed", "data": "m1"}
        mgr.send_mixed_audio(mixed_payload)
        mock_mixed.start.assert_called_once()
        mock_mixed.send_async.assert_called_once_with(mixed_payload)

        pp_audio_payload = {"trigger": "pp_audio", "participant": "p1"}
        mgr.send_per_participant_audio(pp_audio_payload)
        mock_pp_audio.start.assert_called_once()
        mock_pp_audio.send_async.assert_called_once_with(pp_audio_payload)

        pp_video_payload = {"trigger": "pp_video", "participant": "p1", "frame": 0}
        mgr.send_per_participant_video(pp_video_payload)
        mock_pp_video.start.assert_called_once()
        mock_pp_video.send_async.assert_called_once_with(pp_video_payload)

        mock_mixed.started.return_value = True
        mock_pp_audio.started.return_value = True
        mock_pp_video.started.return_value = True

        mgr.send_mixed_audio({"data": "m2"})
        mgr.send_per_participant_audio({"participant": "p2"})
        mgr.send_per_participant_video({"participant": "p2", "frame": 1})
        self.assertEqual(mock_mixed.start.call_count, 1)
        self.assertEqual(mock_pp_audio.start.call_count, 1)
        self.assertEqual(mock_pp_video.start.call_count, 1)
        self.assertEqual(mock_mixed.send_async.call_count, 2)
        self.assertEqual(mock_pp_audio.send_async.call_count, 2)
        self.assertEqual(mock_pp_video.send_async.call_count, 2)

        mgr.cleanup()
        mock_mixed.cleanup.assert_called_once()
        mock_pp_audio.cleanup.assert_called_once()
        mock_pp_video.cleanup.assert_called_once()

    def test_full_lifecycle_shared_client(self, MockClient):
        """Test a full lifecycle with a shared client: init -> send all types -> cleanup."""
        mock_client = Mock(spec=BotWebsocketClient, websocket_url=SHARED_URL)
        started = False

        def fake_started():
            return started

        def fake_start():
            nonlocal started
            started = True

        mock_client.started = fake_started
        mock_client.start = Mock(side_effect=fake_start)
        MockClient.return_value = mock_client

        mgr = BotWebsocketClientManager(
            mixed_audio_url=SHARED_URL,
            per_participant_audio_url=SHARED_URL,
            per_participant_video_url=SHARED_URL,
            on_message_callback=self.mock_callback,
        )

        self.assertEqual(len(mgr._clients), 1)
        self.assertIs(mgr._mixed_audio_client, mgr._per_participant_audio_client)
        self.assertIs(mgr._mixed_audio_client, mgr._per_participant_video_client)

        mgr.send_mixed_audio({"trigger": "mixed"})
        mock_client.start.assert_called_once()

        mgr.send_per_participant_audio({"trigger": "pp_audio"})
        mock_client.start.assert_called_once()

        mgr.send_per_participant_video({"trigger": "pp_video"})
        mock_client.start.assert_called_once()

        self.assertEqual(mock_client.send_async.call_count, 3)

        mgr.cleanup()
        mock_client.cleanup.assert_called_once()

    def test_sends_noop_after_no_url_init(self, MockClient):
        """Test that all send methods are harmless no-ops when no URLs were configured."""
        mgr = BotWebsocketClientManager(
            mixed_audio_url=None,
            per_participant_audio_url=None,
            per_participant_video_url=None,
            on_message_callback=self.mock_callback,
        )

        mgr.send_mixed_audio({"data": "x"})
        mgr.send_per_participant_audio({"data": "y"})
        mgr.send_per_participant_video({"data": "z"})
        mgr.cleanup()

        MockClient.assert_not_called()


if __name__ == "__main__":
    unittest.main()
