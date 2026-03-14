from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from bots.bot_controller.bot_controller import BotController
from bots.websocket_payloads import per_participant_audio_websocket_payload


class PerParticipantAudioWebsocketPayloadTest(TestCase):
    def test_per_participant_audio_websocket_payload_includes_participant_name(self):
        payload = per_participant_audio_websocket_payload(
            participant_uuid="speaker-123",
            participant_name="Test Participant",
            chunk=b"\x01\x02\x03\x04",
            input_sample_rate=16000,
            output_sample_rate=16000,
            bot_object_id="bot_123",
        )

        self.assertEqual(payload["data"]["participant_uuid"], "speaker-123")
        self.assertEqual(payload["data"]["participant_name"], "Test Participant")
        self.assertEqual(payload["data"]["sample_rate"], 16000)

    @patch("bots.bot_controller.bot_controller.per_participant_audio_websocket_payload")
    def test_bot_controller_sender_passes_participant_name(self, mock_payload):
        controller = BotController.__new__(BotController)
        controller.websocket_client_manager = MagicMock()
        controller.get_participant = MagicMock(return_value={"participant_full_name": "Test Participant"})
        controller.get_per_participant_audio_sample_rate = MagicMock(return_value=16000)
        controller.bot_in_db = SimpleNamespace(
            object_id="bot_123",
            websocket_per_participant_audio_sample_rate=lambda: 16000,
        )

        mock_payload.return_value = {"trigger": "realtime_audio.per_participant", "data": {}}

        controller.send_per_participant_audio_chunk_to_websocket_client(
            speaker_id="speaker-123",
            chunk_time=0,
            chunk_bytes=b"\x01\x02\x03\x04",
        )

        mock_payload.assert_called_once_with(
            participant_uuid="speaker-123",
            participant_name="Test Participant",
            chunk=b"\x01\x02\x03\x04",
            input_sample_rate=16000,
            output_sample_rate=16000,
            bot_object_id="bot_123",
        )
        controller.websocket_client_manager.send_per_participant_audio.assert_called_once_with(mock_payload.return_value)
