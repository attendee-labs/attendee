import asyncio
import base64
import json
from unittest.mock import MagicMock

from django.test import SimpleTestCase
from websockets.asyncio.server import serve

from bots.zoom_rtms_adapter.zoom_rtms_adapter import RTMSClient

# Zoom sent 1.35 and 1.49 MB messages in dev meetings; websockets' default limit is 1 MiB.
FRAME = b"\x00" * 1_200_000
BIG_VIDEO_MESSAGE = json.dumps({"msg_type": 15, "content": {"user_id": 7, "user_name": "Ann", "data": base64.b64encode(FRAME).decode()}})


def make_client(port, adapter, use_video):
    return RTMSClient(
        zoom_client_id="client-id",
        zoom_client_secret="client-secret",
        join_payload={"meeting_uuid": "uuid==", "rtms_stream_id": "stream", "server_urls": f"ws://127.0.0.1:{port}"},
        use_audio=True,
        use_video=use_video,
        use_transcript=False,
        use_per_participant_audio=False,
        adapter=adapter,
    )


def run_against(zoom, connect):
    """Serve `zoom` locally and run `connect(port)` against it, failing instead of hanging."""

    async def run():
        async with serve(zoom, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            await connect(port)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


class TestMediaSocketMessageSize(SimpleTestCase):
    def test_video_message_over_one_mib_is_dispatched(self):
        async def zoom(ws):
            await ws.recv()  # handshake
            await ws.send(BIG_VIDEO_MESSAGE)
            await ws.send(BIG_VIDEO_MESSAGE)

        adapter = MagicMock()
        adapter.video_frame_size = (1280, 720)

        async def connect(port):
            await make_client(port, adapter, use_video=True)._connect_media(f"ws://127.0.0.1:{port}")

        with self.assertLogs("bots.zoom_rtms_adapter.zoom_rtms_adapter", level="WARNING") as logs:
            run_against(zoom, connect)

        adapter.handle_fatal_media_socket_error.assert_not_called()
        self.assertEqual(adapter._on_video_frame.call_count, 2)
        adapter._on_video_frame.assert_called_with(FRAME, "Ann", 7)
        large_warnings = [line for line in logs.output if "Large media message" in line]
        self.assertEqual(len(large_warnings), 1, "large-message warning should be logged once per socket")


class TestSignalingSocketMessageSize(SimpleTestCase):
    def test_participant_event_over_one_mib_is_dispatched(self):
        participants = [{"user_id": i, "user_name": "x" * 100} for i in range(12_000)]
        big_join = json.dumps({"msg_type": 6, "event": {"event_type": 3, "participants": participants}})
        self.assertGreater(len(big_join), 1024 * 1024)

        async def zoom(ws):
            await ws.recv()  # handshake
            await ws.send(big_join)

        adapter = MagicMock()

        async def connect(port):
            await make_client(port, adapter, use_video=False)._connect_signaling()

        run_against(zoom, connect)

        self.assertEqual(adapter.post_rtms_event.call_count, len(participants))
