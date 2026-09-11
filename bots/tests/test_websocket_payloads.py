import time
import unittest
from base64 import b64encode
from unittest.mock import Mock, patch

from bots.bot_controller.bot_controller import BotController
from bots.websocket_payloads import per_participant_audio_websocket_payload, per_participant_video_websocket_payload

BOT_OBJECT_ID = "bot_abc123"
PARTICIPANT_UUID = "participant_abc123"
FAKE_JPEG_B64 = b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9")
FROZEN_TIME = 1703123456.789
FROZEN_TIMESTAMP_MS = 1703123456789


class TestPerParticipantVideoWebsocketPayload(unittest.TestCase):
    @patch("bots.websocket_payloads.time.time", return_value=FROZEN_TIME)
    def test_payload_contains_timestamp_ms_alongside_existing_fields(self, mock_time):
        payload = per_participant_video_websocket_payload(frame=FAKE_JPEG_B64, bot_object_id=BOT_OBJECT_ID, participant_uuid=PARTICIPANT_UUID, source="webcam")

        self.assertEqual(payload["trigger"], "realtime_video.per_participant")
        self.assertEqual(payload["bot_id"], BOT_OBJECT_ID)
        self.assertEqual(payload["data"]["frame"], FAKE_JPEG_B64.decode("ascii"))
        self.assertEqual(payload["data"]["format"], "jpeg")
        self.assertEqual(payload["data"]["participant_uuid"], PARTICIPANT_UUID)
        self.assertEqual(payload["data"]["source"], "webcam")
        self.assertEqual(payload["data"]["timestamp_ms"], FROZEN_TIMESTAMP_MS)
        self.assertEqual(set(payload["data"].keys()), {"frame", "format", "participant_uuid", "source", "timestamp_ms"})

    def test_timestamp_ms_is_integer_epoch_milliseconds(self):
        before_ms = int(time.time() * 1000)
        payload = per_participant_video_websocket_payload(frame=FAKE_JPEG_B64, bot_object_id=BOT_OBJECT_ID, participant_uuid=PARTICIPANT_UUID, source="screenshare")
        after_ms = int(time.time() * 1000)

        self.assertIsInstance(payload["data"]["timestamp_ms"], int)
        self.assertGreaterEqual(payload["data"]["timestamp_ms"], before_ms)
        self.assertLessEqual(payload["data"]["timestamp_ms"], after_ms)

    @patch("bots.websocket_payloads.time.time", return_value=FROZEN_TIME)
    def test_timestamp_ms_matches_per_participant_audio_convention(self, mock_time):
        audio_payload = per_participant_audio_websocket_payload(participant_uuid=PARTICIPANT_UUID, chunk=b"\x00\x00" * 160, input_sample_rate=16000, output_sample_rate=16000, bot_object_id=BOT_OBJECT_ID)
        video_payload = per_participant_video_websocket_payload(frame=FAKE_JPEG_B64, bot_object_id=BOT_OBJECT_ID, participant_uuid=PARTICIPANT_UUID, source="webcam")

        self.assertEqual(video_payload["data"]["timestamp_ms"], audio_payload["data"]["timestamp_ms"])

    def test_invalid_source_raises(self):
        with self.assertRaises(ValueError):
            per_participant_video_websocket_payload(frame=FAKE_JPEG_B64, bot_object_id=BOT_OBJECT_ID, participant_uuid=PARTICIPANT_UUID, source="microphone")


class TestAddPerParticipantVideoFrameCallback(unittest.TestCase):
    def _make_controller(self, websocket_client_manager):
        controller = BotController.__new__(BotController)
        controller.websocket_client_manager = websocket_client_manager
        controller.bot_in_db = Mock(object_id=BOT_OBJECT_ID)
        return controller

    @patch("bots.websocket_payloads.time.time", return_value=FROZEN_TIME)
    def test_sends_timestamped_payload_to_websocket_client_manager(self, mock_time):
        websocket_client_manager = Mock()
        controller = self._make_controller(websocket_client_manager)

        controller.add_per_participant_video_frame_callback(FAKE_JPEG_B64, PARTICIPANT_UUID, "screenshare")

        websocket_client_manager.send_per_participant_video.assert_called_once_with(
            {
                "trigger": "realtime_video.per_participant",
                "bot_id": BOT_OBJECT_ID,
                "data": {
                    "frame": FAKE_JPEG_B64.decode("ascii"),
                    "format": "jpeg",
                    "participant_uuid": PARTICIPANT_UUID,
                    "source": "screenshare",
                    "timestamp_ms": FROZEN_TIMESTAMP_MS,
                },
            }
        )

    def test_stamps_each_frame_when_it_is_received(self):
        websocket_client_manager = Mock()
        controller = self._make_controller(websocket_client_manager)

        with patch("bots.websocket_payloads.time.time", side_effect=[FROZEN_TIME, FROZEN_TIME + 0.5]):
            controller.add_per_participant_video_frame_callback(FAKE_JPEG_B64, PARTICIPANT_UUID, "webcam")
            controller.add_per_participant_video_frame_callback(FAKE_JPEG_B64, PARTICIPANT_UUID, "webcam")

        sent_timestamps = [call.args[0]["data"]["timestamp_ms"] for call in websocket_client_manager.send_per_participant_video.call_args_list]
        self.assertEqual(sent_timestamps, [FROZEN_TIMESTAMP_MS, FROZEN_TIMESTAMP_MS + 500])

    @patch("bots.bot_controller.bot_controller.per_participant_video_websocket_payload")
    def test_does_nothing_without_websocket_client_manager(self, mock_payload):
        controller = self._make_controller(None)

        controller.add_per_participant_video_frame_callback(FAKE_JPEG_B64, PARTICIPANT_UUID, "webcam")

        mock_payload.assert_not_called()
