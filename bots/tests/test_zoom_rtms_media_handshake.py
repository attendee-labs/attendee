from django.test import SimpleTestCase

from bots.zoom_rtms_adapter.zoom_rtms_adapter import build_media_handshake


class TestBuildMediaHandshake(SimpleTestCase):
    def build(self, **overrides):
        kwargs = dict(
            meeting_uuid="meeting-uuid==",
            stream_id="stream-id",
            signature="signature",
            use_video=False,
            use_transcript=False,
            use_per_participant_audio=True,
            video_frame_size=(1280, 720),
        )
        kwargs.update(overrides)
        return build_media_handshake(**kwargs)

    def test_carries_the_stream_identity_and_signature(self):
        handshake = self.build()

        self.assertEqual(handshake["msg_type"], 3)
        self.assertEqual(handshake["protocol_version"], 1)
        self.assertEqual(handshake["meeting_uuid"], "meeting-uuid==")
        self.assertEqual(handshake["rtms_stream_id"], "stream-id")
        self.assertEqual(handshake["signature"], "signature")
        self.assertFalse(handshake["payload_encryption"])

    def test_audio_and_chat_request_all_media_types(self):
        # Zoom answers a combined mask such as 17 (audio | chat) with "Media type invalid value"
        # and stops the stream; one socket takes a single type or 32 for all of them.
        handshake = self.build()

        self.assertEqual(handshake["media_type"], 32)
        self.assertEqual(handshake["media_params"]["chat"], {"content_type": 5})

    def test_per_participant_audio_requests_one_stream_per_speaker(self):
        audio = self.build(use_per_participant_audio=True)["media_params"]["audio"]

        self.assertEqual(audio["data_opt"], 2)
        self.assertEqual(audio["content_type"], 2)
        self.assertEqual(audio["codec"], 1)
        self.assertEqual(audio["sample_rate"], 1)
        self.assertEqual(audio["channel"], 1)

    def test_mixed_audio_requests_the_mixed_stream(self):
        audio = self.build(use_per_participant_audio=False)["media_params"]["audio"]

        self.assertEqual(audio["data_opt"], 1)

    def test_video_keeps_the_audio_and_video_params(self):
        handshake = self.build(use_video=True, use_per_participant_audio=False, video_frame_size=(1920, 1080))

        self.assertEqual(handshake["media_type"], 32)
        self.assertEqual(handshake["media_params"]["audio"]["data_opt"], 1)
        self.assertEqual(handshake["media_params"]["audio"]["send_rate"], 20)
        self.assertEqual(handshake["media_params"]["video"]["resolution"], 3)

    def test_video_rejects_an_unsupported_frame_size(self):
        with self.assertRaises(ValueError):
            self.build(use_video=True, video_frame_size=(640, 480))
