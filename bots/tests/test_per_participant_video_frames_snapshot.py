import json
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase

from accounts.models import Organization
from bots.bot_adapter import BotAdapter
from bots.bot_controller.bot_controller import BotController
from bots.models import (
    ApiKey,
    Bot,
    BotStates,
    Participant,
    PerParticipantVideoFramesSnapshotRequest,
    PerParticipantVideoFramesSnapshotRequestManager,
    PerParticipantVideoFramesSnapshotRequestStates,
    Project,
)
from bots.per_participant_realtime_video_configuration import PerParticipantRealtimeVideoConfiguration, PerParticipantRealtimeVideoSourceConfiguration
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


class PerParticipantVideoFramesSnapshotViewTest(TestCase):
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

    def _request_snapshot(self, data, bot_side_effect=None):
        def send_sync_command_side_effect(bot, command):
            if bot_side_effect:
                snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.get(bot=bot)
                PerParticipantVideoFramesSnapshotRequestManager.set_request_in_progress(snapshot_request)
                snapshot_request.refresh_from_db()
                bot_side_effect(snapshot_request)

        with patch("bots.bots_api_views.send_sync_command", side_effect=send_sync_command_side_effect) as mock_send_sync_command, patch("bots.bots_api_views.time.sleep") as mock_sleep:
            response = self.client.post(
                f"/api/v1/bots/{self.bot.object_id}/per_participant_video_frames_snapshot",
                data=json.dumps(data),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
            )
        return response, mock_send_sync_command, mock_sleep

    def test_returns_frames_filled_in_by_bot_and_clears_them(self):
        def fulfill(snapshot_request):
            PerParticipantVideoFramesSnapshotRequestManager.set_request_fulfilled(snapshot_request, [stored_frame("16778240"), stored_frame("99999999", source="webcam")])

        response, mock_send_sync_command, _ = self._request_snapshot({}, bot_side_effect=fulfill)

        self.assertEqual(response.status_code, 200, response.content)
        mock_send_sync_command.assert_called_once_with(self.bot, "sync_per_participant_video_frames_snapshot_requests")
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

        snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.get(bot=self.bot)
        self.assertEqual(snapshot_request.state, PerParticipantVideoFramesSnapshotRequestStates.FULFILLED)
        self.assertIsNone(snapshot_request.frames)
        # Webcams are only captured when requested
        self.assertEqual(snapshot_request.webcam_resolution, "none")
        self.assertEqual(snapshot_request.screenshare_resolution, "360p")

    def test_stores_requested_resolutions(self):
        def fulfill(snapshot_request):
            PerParticipantVideoFramesSnapshotRequestManager.set_request_fulfilled(snapshot_request, [])

        response, _, _ = self._request_snapshot({"webcam_resolution": "720p", "screenshare_resolution": "1080p"}, bot_side_effect=fulfill)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"frames": []})
        snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.get(bot=self.bot)
        self.assertEqual(snapshot_request.webcam_resolution, "720p")
        self.assertEqual(snapshot_request.screenshare_resolution, "1080p")

    def test_times_out_if_bot_does_not_fill_in_request(self):
        response, _, mock_sleep = self._request_snapshot({})

        self.assertEqual(response.status_code, 504, response.content)
        self.assertEqual(mock_sleep.call_count, 25)
        snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.get(bot=self.bot)
        self.assertEqual(snapshot_request.state, PerParticipantVideoFramesSnapshotRequestStates.FAILED)

        # A result that arrives after the timeout is not stored
        self.assertFalse(PerParticipantVideoFramesSnapshotRequestManager.set_request_fulfilled(snapshot_request, [stored_frame("16778240")]))
        snapshot_request.refresh_from_db()
        self.assertIsNone(snapshot_request.frames)

    def test_returns_error_when_bot_fails_to_capture(self):
        def fail(snapshot_request):
            PerParticipantVideoFramesSnapshotRequestManager.set_request_failed(snapshot_request, {"description": "Bot is not recording"})

        response, _, _ = self._request_snapshot({}, bot_side_effect=fail)

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), {"error": "Failed to capture per participant video frames: Bot is not recording"})

    def test_rejects_unsupported_bots(self):
        for meeting_url, settings in [
            ("https://zoom.us/j/123456789", {}),
            ("https://meet.google.com/abc-defg-hij", {}),
        ]:
            with self.subTest(meeting_url=meeting_url, settings=settings):
                self.bot.meeting_url = meeting_url
                self.bot.settings = settings
                self.bot.save()

                response, mock_send_sync_command, _ = self._request_snapshot({})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json(), {"error": "Capturing per participant video frames snapshots is only supported for Zoom Web SDK"})
                mock_send_sync_command.assert_not_called()
                self.assertFalse(PerParticipantVideoFramesSnapshotRequest.objects.exists())

    def test_rejects_bot_that_is_not_recording(self):
        for state in [BotStates.JOINING, BotStates.JOINED_RECORDING_PAUSED, BotStates.ENDED]:
            with self.subTest(state=state):
                self.bot.state = state
                self.bot.save()

                response, mock_send_sync_command, _ = self._request_snapshot({})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("cannot capture per participant video frames", response.json()["error"])
                mock_send_sync_command.assert_not_called()

    def test_rejects_invalid_resolutions(self):
        for data in [{"webcam_resolution": "none", "screenshare_resolution": "none"}, {"screenshare_resolution": "4k"}]:
            with self.subTest(data=data):
                response, mock_send_sync_command, _ = self._request_snapshot(data)

                self.assertEqual(response.status_code, 400, response.content)
                mock_send_sync_command.assert_not_called()

    def test_returns_404_for_unknown_bot(self):
        response = self.client.post(
            "/api/v1/bots/bot_doesnotexist/per_participant_video_frames_snapshot",
            data=json.dumps({}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
        )
        self.assertEqual(response.status_code, 404)


class PerParticipantVideoFramesSnapshotBotTest(TestCase):
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

    def test_bot_controller_sends_enqueued_requests_to_adapter_once(self):
        snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="1080p")

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        controller.take_action_based_on_per_participant_video_frames_snapshot_requests_in_db()
        # A second sync while the first capture is in progress doesn't capture again
        controller.take_action_based_on_per_participant_video_frames_snapshot_requests_in_db()

        controller.adapter.capture_per_participant_video_frames_snapshot.assert_called_once_with(
            request_id=snapshot_request.id,
            per_participant_realtime_video_configuration=PerParticipantRealtimeVideoConfiguration(
                webcam_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="none"),
                screenshare_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="1080p"),
            ),
        )

    def test_bot_controller_fails_requests_when_bot_is_not_recording(self):
        self.bot.state = BotStates.JOINED_RECORDING_PAUSED
        self.bot.save()
        snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="360p")

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()

        controller.take_action_based_on_per_participant_video_frames_snapshot_requests_in_db()

        controller.adapter.capture_per_participant_video_frames_snapshot.assert_not_called()
        snapshot_request.refresh_from_db()
        self.assertEqual(snapshot_request.state, PerParticipantVideoFramesSnapshotRequestStates.FAILED)

    def test_bot_controller_stores_result_from_adapter(self):
        fulfilled_request = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="360p")
        failed_request = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="360p")

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()
        controller.take_action_based_on_per_participant_video_frames_snapshot_requests_in_db()

        controller.take_action_based_on_message_from_adapter({"message": BotAdapter.Messages.PER_PARTICIPANT_VIDEO_FRAMES_SNAPSHOT_RESULT, "request_id": fulfilled_request.id, "error": None, "frames": [stored_frame("16778240")]})
        controller.take_action_based_on_message_from_adapter({"message": BotAdapter.Messages.PER_PARTICIPANT_VIDEO_FRAMES_SNAPSHOT_RESULT, "request_id": failed_request.id, "error": "Bot is not recording", "frames": []})

        fulfilled_request.refresh_from_db()
        self.assertEqual(fulfilled_request.state, PerParticipantVideoFramesSnapshotRequestStates.FULFILLED)
        self.assertEqual(fulfilled_request.frames, [stored_frame("16778240")])
        failed_request.refresh_from_db()
        self.assertEqual(failed_request.state, PerParticipantVideoFramesSnapshotRequestStates.FAILED)
        self.assertEqual(failed_request.failure_data, {"description": "Bot is not recording"})

    def test_zoom_web_adapter_asks_page_for_snapshot(self):
        # Skip __init__, which sets up the browser; capture_per_participant_video_frames_snapshot only needs the driver
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.driver = MagicMock()
        configuration = PerParticipantRealtimeVideoConfiguration(
            webcam_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="none"),
            screenshare_configuration=PerParticipantRealtimeVideoSourceConfiguration(resolution="720p"),
        )

        adapter.capture_per_participant_video_frames_snapshot(request_id=42, per_participant_realtime_video_configuration=configuration)

        adapter.driver.execute_script.assert_called_once_with("window?.capturePerParticipantVideoFramesSnapshot(arguments[0], arguments[1]);", 42, configuration.to_dict())

    def test_web_adapter_converts_page_result_to_frames(self):
        adapter = ZoomWebBotAdapter.__new__(ZoomWebBotAdapter)
        adapter.send_message_callback = MagicMock()

        adapter.handle_per_participant_video_frames_snapshot_result(
            {
                "type": "PerParticipantVideoFramesSnapshotResult",
                "requestId": 42,
                "frames": [{"participantId": "16778240", "isScreenShare": True, "frame": "/9j/4AAQSkZJRgABAQAAAQABAAD", "width": 640, "height": 360, "timestamp": 1764177600000}],
            }
        )

        adapter.send_message_callback.assert_called_once_with(
            {
                "message": BotAdapter.Messages.PER_PARTICIPANT_VIDEO_FRAMES_SNAPSHOT_RESULT,
                "request_id": 42,
                "error": None,
                "frames": [stored_frame("16778240")],
            }
        )

    def test_bot_controller_dispatches_all_enqueued_requests(self):
        request_1 = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="360p")
        request_2 = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="1080p")

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()
        controller.take_action_based_on_per_participant_video_frames_snapshot_requests_in_db()

        self.assertEqual(controller.adapter.capture_per_participant_video_frames_snapshot.call_count, 2)
        request_1.refresh_from_db()
        request_2.refresh_from_db()
        self.assertEqual(request_1.state, PerParticipantVideoFramesSnapshotRequestStates.IN_PROGRESS)
        self.assertEqual(request_2.state, PerParticipantVideoFramesSnapshotRequestStates.IN_PROGRESS)

    def test_bot_controller_does_not_double_dispatch_in_progress_request(self):
        snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="360p")
        PerParticipantVideoFramesSnapshotRequestManager.set_request_in_progress(snapshot_request)

        controller = BotController(self.bot.id)
        controller.adapter = MagicMock()
        controller.take_action_based_on_per_participant_video_frames_snapshot_requests_in_db()

        controller.adapter.capture_per_participant_video_frames_snapshot.assert_not_called()

    def test_result_after_timeout_is_discarded(self):
        snapshot_request = PerParticipantVideoFramesSnapshotRequest.objects.create(bot=self.bot, webcam_resolution="none", screenshare_resolution="360p")
        PerParticipantVideoFramesSnapshotRequestManager.set_request_in_progress(snapshot_request)
        PerParticipantVideoFramesSnapshotRequestManager.set_request_failed(snapshot_request, {"description": "Timed out"})

        result = PerParticipantVideoFramesSnapshotRequestManager.set_request_fulfilled(snapshot_request, [stored_frame("16778240")])
        snapshot_request.refresh_from_db()

        self.assertFalse(result)
        self.assertIsNone(snapshot_request.frames)
        self.assertEqual(snapshot_request.state, PerParticipantVideoFramesSnapshotRequestStates.FAILED)
