import json
import time
from unittest.mock import MagicMock, patch

import redis
from django.test import Client, TestCase

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.models import (
    ApiKey,
    Bot,
    BotStates,
    Participant,
    Project,
)
from bots.per_participant_realtime_video_configuration import PerParticipantRealtimeVideoConfiguration, PerParticipantRealtimeVideoSourceConfiguration
from bots.per_participant_video_snapshot_store import PerParticipantVideoSnapshotResult, PerParticipantVideoSnapshotStore
from bots.zoom_web_bot_adapter.zoom_web_bot_adapter import ZoomWebBotAdapter


def stored_frame(participant_uuid, source="screenshare"):
    return {
        "participant_uuid": participant_uuid,
        "source": source,
        "frame": "/9j/4AAQSkZJRgABAQAAAQABAAD",
        "format": "jpeg",
        "width": 640,
        "height": 360,
        "timestamp_ms": 1764177600000,
    }


def clear_snapshot_keys(bot):
    with PerParticipantVideoSnapshotStore() as snapshot_store:
        snapshot_store._redis_client.delete(PerParticipantVideoSnapshotStore._request_key(bot.id))


class PerParticipantVideoSnapshotViewTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org", centicredits=10000)
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.api_key, self.api_key_plain = ApiKey.create(project=self.project, name="Test API Key")
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://zoom.us/j/123456789",
            state=BotStates.JOINED_RECORDING,
            settings={"zoom_settings": {"sdk": "web"}},
        )
        self.participant = Participant.objects.create(bot=self.bot, uuid="16778240", user_uuid="user-uuid-1", full_name="Alice")
        self.client = Client()

    def tearDown(self):
        clear_snapshot_keys(self.bot)

    def _request_snapshot(self, data, bot_side_effect=None):
        # Stands in for the bot: it reads the request from Redis and passes it to bot_side_effect
        def send_sync_command_side_effect(bot, command):
            if bot_side_effect:
                with PerParticipantVideoSnapshotStore() as snapshot_store:
                    bot_side_effect(snapshot_store, snapshot_store.get_request(bot.id))

        with patch("bots.bots_api_views.send_sync_command", side_effect=send_sync_command_side_effect) as mock_send_sync_command:
            response = self.client.post(
                f"/api/v1/bots/{self.bot.object_id}/per_participant_video_snapshot",
                data=json.dumps(data),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
            )
        return response, mock_send_sync_command

    def test_returns_frames_filled_in_by_bot_and_clears_them(self):
        captured_requests = []

        def fulfill(snapshot_store, snapshot_request):
            captured_requests.append(snapshot_request)
            snapshot_store.set_result(snapshot_request.id, frames=[stored_frame("16778240"), stored_frame("99999999", source="webcam")])

        response, mock_send_sync_command = self._request_snapshot({}, bot_side_effect=fulfill)

        self.assertEqual(response.status_code, 200, response.content)
        mock_send_sync_command.assert_called_once_with(self.bot, "sync_per_participant_video_snapshot_requests")
        self.assertEqual(
            response.json(),
            {
                "frames": [
                    {**stored_frame("16778240"), "participant_user_uuid": "user-uuid-1", "participant_name": "Alice"},
                    # Participants we don't know about yet are still returned
                    {**stored_frame("99999999", source="webcam"), "participant_user_uuid": None, "participant_name": None},
                ]
            },
        )

        # Webcams are only captured when requested
        self.assertEqual(captured_requests[0].webcam_resolution, "none")
        self.assertEqual(captured_requests[0].screenshare_resolution, "360p")

        # Both the request and the result are removed from Redis
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            self.assertIsNone(snapshot_store.get_request(self.bot.id))
            self.assertFalse(snapshot_store._redis_client.exists(PerParticipantVideoSnapshotStore._result_key(captured_requests[0].id)))

    def test_passes_requested_resolutions_to_bot(self):
        captured_requests = []

        def fulfill(snapshot_store, snapshot_request):
            captured_requests.append(snapshot_request)
            snapshot_store.set_result(snapshot_request.id, frames=[])

        response, _ = self._request_snapshot({"webcam_resolution": "720p", "screenshare_resolution": "1080p"}, bot_side_effect=fulfill)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"frames": []})
        self.assertEqual(captured_requests[0].webcam_resolution, "720p")
        self.assertEqual(captured_requests[0].screenshare_resolution, "1080p")

    def test_can_capture_again_after_a_snapshot_finishes(self):
        def fulfill(snapshot_store, snapshot_request):
            snapshot_store.set_result(snapshot_request.id, frames=[])

        for _ in range(2):
            response, _ = self._request_snapshot({}, bot_side_effect=fulfill)
            self.assertEqual(response.status_code, 200, response.content)

    def test_rejects_request_while_another_snapshot_is_pending(self):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            snapshot_store.create_request(self.bot.id, webcam_resolution="none", screenshare_resolution="360p")

        response, mock_send_sync_command = self._request_snapshot({})

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("already capturing", response.json()["error"])
        mock_send_sync_command.assert_not_called()

    def test_times_out_if_bot_does_not_fill_in_request(self):
        captured_requests = []

        def capture_slowly(snapshot_store, snapshot_request):
            captured_requests.append(snapshot_request)

        with patch("bots.bots_api_views.PerParticipantVideoSnapshotView.RESULT_TIMEOUT_SECONDS", 1):
            start = time.monotonic()
            response, _ = self._request_snapshot({}, bot_side_effect=capture_slowly)
            elapsed = time.monotonic() - start

        self.assertEqual(response.status_code, 504, response.content)
        # Waited for the result rather than giving up straight away
        self.assertGreaterEqual(elapsed, 0.9)

        # The bot may still be capturing, so the request is left to expire rather than letting a new snapshot overlap with it
        response, mock_send_sync_command = self._request_snapshot({})
        self.assertEqual(response.status_code, 403, response.content)
        mock_send_sync_command.assert_not_called()
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            self.assertEqual(snapshot_store.get_request(self.bot.id), captured_requests[0])
            self.assertLessEqual(snapshot_store._redis_client.ttl(PerParticipantVideoSnapshotStore._request_key(self.bot.id)), PerParticipantVideoSnapshotStore.REQUEST_TTL_SECONDS)

    def test_returns_error_when_bot_fails_to_capture(self):
        def fail(snapshot_store, snapshot_request):
            snapshot_store.set_result(snapshot_request.id, error="Bot is not recording")

        response, _ = self._request_snapshot({}, bot_side_effect=fail)

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"error": "Failed to capture per participant video frames: Bot is not recording"})
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            self.assertIsNone(snapshot_store.get_request(self.bot.id))

    def test_releases_request_if_bot_cannot_be_notified(self):
        with patch("bots.bots_api_views.send_sync_command", side_effect=Exception("Redis is down")):
            response = self.client.post(
                f"/api/v1/bots/{self.bot.object_id}/per_participant_video_snapshot",
                data=json.dumps({}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
            )

        self.assertEqual(response.status_code, 500, response.content)
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            self.assertIsNone(snapshot_store.get_request(self.bot.id))

    def test_rejects_unsupported_bots(self):
        for meeting_url, settings in [
            ("https://zoom.us/j/123456789", {}),
            ("https://meet.google.com/abc-defg-hij", {}),
        ]:
            with self.subTest(meeting_url=meeting_url, settings=settings):
                self.bot.meeting_url = meeting_url
                self.bot.settings = settings
                self.bot.save()

                response, mock_send_sync_command = self._request_snapshot({})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json(), {"error": "Capturing per participant video snapshots is only supported for Zoom Web SDK"})
                mock_send_sync_command.assert_not_called()
                with PerParticipantVideoSnapshotStore() as snapshot_store:
                    self.assertIsNone(snapshot_store.get_request(self.bot.id))

    def test_rejects_bot_that_is_not_recording(self):
        for state in [BotStates.JOINING, BotStates.JOINED_RECORDING_PAUSED, BotStates.ENDED]:
            with self.subTest(state=state):
                self.bot.state = state
                self.bot.save()

                response, mock_send_sync_command = self._request_snapshot({})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("cannot capture per participant video frames", response.json()["error"])
                mock_send_sync_command.assert_not_called()

    def test_rejects_invalid_resolutions(self):
        for data in [{"webcam_resolution": "none", "screenshare_resolution": "none"}, {"screenshare_resolution": "4k"}]:
            with self.subTest(data=data):
                response, mock_send_sync_command = self._request_snapshot(data)

                self.assertEqual(response.status_code, 400, response.content)
                mock_send_sync_command.assert_not_called()

    def test_returns_404_for_unknown_bot(self):
        response = self.client.post(
            "/api/v1/bots/bot_doesnotexist/per_participant_video_snapshot",
            data=json.dumps({}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
        )
        self.assertEqual(response.status_code, 404)


class PerParticipantVideoSnapshotBotTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org", centicredits=10000)
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://zoom.us/j/123456789",
            state=BotStates.JOINED_RECORDING,
            settings={"zoom_settings": {"sdk": "web"}},
        )

    def tearDown(self):
        clear_snapshot_keys(self.bot)

    def _create_request(self, screenshare_resolution="360p"):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            return snapshot_store.create_request(self.bot.id, webcam_resolution="none", screenshare_resolution=screenshare_resolution)

    def _wait_for_result(self, request_id):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            return snapshot_store.wait_for_result(request_id, timeout_seconds=1)

    def test_bot_controller_sends_request_to_adapter_once(self):
        snapshot_request = self._create_request(screenshare_resolution="1080p")

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        controller.take_action_based_on_per_participant_video_snapshot_request()
        # A second sync while the first capture is in progress doesn't capture again
        controller.take_action_based_on_per_participant_video_snapshot_request()

        controller.adapter.capture_per_participant_video_snapshot.assert_called_once_with(
            request_id=snapshot_request.id,
            per_participant_realtime_video_configuration=PerParticipantRealtimeVideoConfiguration(
                webcam_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="none"),
                screenshare_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="1080p"),
            ),
        )

    def test_bot_controller_does_nothing_without_a_request(self):
        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        controller.take_action_based_on_per_participant_video_snapshot_request()

        controller.adapter.capture_per_participant_video_snapshot.assert_not_called()

    def test_bot_controller_fails_request_when_bot_is_not_recording(self):
        self.bot.state = BotStates.JOINED_RECORDING_PAUSED
        self.bot.save()
        snapshot_request = self._create_request()

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        controller.take_action_based_on_per_participant_video_snapshot_request()

        controller.adapter.capture_per_participant_video_snapshot.assert_not_called()
        self.assertEqual(self._wait_for_result(snapshot_request.id), PerParticipantVideoSnapshotResult(frames=None, error="Bot is in state joined_recording_paused and cannot capture per participant video frames"))

    def test_bot_controller_stores_result_from_adapter(self):
        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        with patch("bots.bot_controller.bot_controller.GLib.idle_add") as mock_idle_add:
            controller.add_per_participant_video_snapshot_callback("fulfilled-request", [stored_frame("16778240")], None)
            controller.add_per_participant_video_snapshot_callback("failed-request", [], "Bot is not recording")

        # The callback runs on the adapter's websocket thread, so it only schedules the Redis write on the main loop
        self.assertFalse(PerParticipantVideoSnapshotStore()._redis_client.exists(PerParticipantVideoSnapshotStore._result_key("fulfilled-request")))
        self.assertEqual(mock_idle_add.call_count, 2)
        for call in mock_idle_add.call_args_list:
            call.args[0]()

        self.assertEqual(self._wait_for_result("fulfilled-request"), PerParticipantVideoSnapshotResult(frames=[stored_frame("16778240")], error=None))
        self.assertEqual(self._wait_for_result("failed-request"), PerParticipantVideoSnapshotResult(frames=None, error="Bot is not recording"))

    def test_bot_controller_survives_failed_redis_write(self):
        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        with patch.object(PerParticipantVideoSnapshotStore, "set_result", side_effect=redis.ConnectionError("Redis is down")):
            # Doesn't raise; the web server just times out waiting for this snapshot
            controller.save_per_participant_video_snapshot_result("some-request", [stored_frame("16778240")], None)

    def test_bot_controller_bounds_redis_write_time(self):
        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        with patch("bots.bot_controller.bot_controller.PerParticipantVideoSnapshotStore") as mock_store_class:
            controller.save_per_participant_video_snapshot_result("some-request", [], None)

        mock_store_class.assert_called_once_with(socket_timeout_seconds=2)

    def test_zoom_web_adapter_asks_page_for_snapshot(self):
        # Skip __init__, which sets up the browser; capture_per_participant_video_snapshot only needs the driver
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.driver = MagicMock()
        configuration = PerParticipantRealtimeVideoConfiguration(
            webcam_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="none"),
            screenshare_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="720p"),
        )

        adapter.capture_per_participant_video_snapshot(request_id=42, per_participant_realtime_video_configuration=configuration)

        adapter.driver.execute_script.assert_called_once_with("window?.capturePerParticipantVideoSnapshot(arguments[0], arguments[1]);", 42, configuration.to_dict())

    def test_web_adapter_converts_page_result_to_frames(self):
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.add_per_participant_video_snapshot_callback = MagicMock()
        adapter.send_message_callback = MagicMock()

        adapter.handle_per_participant_video_snapshot_result(
            {
                "type": "PerParticipantVideoSnapshotResult",
                "requestId": "abc123",
                "frames": [{"participantId": "16778240", "isScreenShare": True, "frame": "/9j/4AAQSkZJRgABAQAAAQABAAD", "width": 640, "height": 360, "timestamp": 1764177600000}],
            }
        )

        # Frames go through the dedicated callback, not the adapter's message channel
        adapter.add_per_participant_video_snapshot_callback.assert_called_once_with("abc123", [stored_frame("16778240")], None)
        adapter.send_message_callback.assert_not_called()

    def test_web_adapter_passes_page_error_to_callback(self):
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.add_per_participant_video_snapshot_callback = MagicMock()

        adapter.handle_per_participant_video_snapshot_result({"type": "PerParticipantVideoSnapshotResult", "requestId": "abc123", "frames": [], "error": "Bot is not recording"})

        adapter.add_per_participant_video_snapshot_callback.assert_called_once_with("abc123", [], "Bot is not recording")

    def test_web_adapter_ignores_malformed_page_result(self):
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.add_per_participant_video_snapshot_callback = MagicMock()

        # Missing every frame field
        adapter.handle_per_participant_video_snapshot_result({"type": "PerParticipantVideoSnapshotResult", "requestId": "abc123", "frames": [{}]})

        adapter.add_per_participant_video_snapshot_callback.assert_not_called()

    def test_failed_snapshot_does_not_interrupt_media_on_websocket(self):
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.add_per_participant_video_snapshot_callback = MagicMock(side_effect=redis.ConnectionError("Redis is down"))
        adapter.process_mixed_audio_frame = MagicMock()

        snapshot_message = (1).to_bytes(4, byteorder="little") + json.dumps({"type": "PerParticipantVideoSnapshotResult", "requestId": "abc123", "frames": []}).encode("utf-8")
        audio_message = (3).to_bytes(4, byteorder="little") + b"\x00" * 16

        adapter.handle_websocket([snapshot_message, audio_message])

        # The audio frame after the failed snapshot is still processed
        adapter.process_mixed_audio_frame.assert_called_once_with(audio_message)


class PerParticipantVideoSnapshotStoreTest(TestCase):
    BOT_ID = -1

    def tearDown(self):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            snapshot_store._redis_client.delete(PerParticipantVideoSnapshotStore._request_key(self.BOT_ID))

    def test_only_one_request_per_bot_at_a_time(self):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            snapshot_request = snapshot_store.create_request(self.BOT_ID, webcam_resolution="none", screenshare_resolution="360p")
            self.assertIsNotNone(snapshot_request)
            self.assertIsNone(snapshot_store.create_request(self.BOT_ID, webcam_resolution="none", screenshare_resolution="360p"))
            self.assertGreater(snapshot_store._redis_client.ttl(PerParticipantVideoSnapshotStore._request_key(self.BOT_ID)), 0)

            snapshot_store.delete_request(self.BOT_ID, snapshot_request.id)
            self.assertIsNotNone(snapshot_store.create_request(self.BOT_ID, webcam_resolution="none", screenshare_resolution="360p"))

    def test_delete_request_ignores_other_requests(self):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            snapshot_request = snapshot_store.create_request(self.BOT_ID, webcam_resolution="none", screenshare_resolution="360p")

            snapshot_store.delete_request(self.BOT_ID, "some-older-request")

            self.assertEqual(snapshot_store.get_request(self.BOT_ID), snapshot_request)

    def test_result_expires_and_is_removed_once_read(self):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            snapshot_store.set_result("test-request", frames=[stored_frame("16778240")])
            self.assertGreater(snapshot_store._redis_client.ttl(PerParticipantVideoSnapshotStore._result_key("test-request")), 0)

            self.assertEqual(snapshot_store.wait_for_result("test-request", timeout_seconds=1), PerParticipantVideoSnapshotResult(frames=[stored_frame("16778240")], error=None))
            self.assertFalse(snapshot_store._redis_client.exists(PerParticipantVideoSnapshotStore._result_key("test-request")))

    def test_wait_for_result_rejects_timeouts_redis_would_misread(self):
        with PerParticipantVideoSnapshotStore() as snapshot_store:
            for timeout_seconds in [0, 0.5]:
                with self.subTest(timeout_seconds=timeout_seconds), self.assertRaises(ValueError):
                    snapshot_store.wait_for_result("test-request", timeout_seconds=timeout_seconds)
