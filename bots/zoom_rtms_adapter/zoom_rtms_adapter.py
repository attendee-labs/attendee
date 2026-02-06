import asyncio
import base64
import hashlib
import hmac
import json
import logging
import ssl
import threading
import time
from datetime import datetime

import gi

from bots.bot_adapter import BotAdapter

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

logger = logging.getLogger(__name__)

# Try import websockets; don't break import-time if it's missing in some environments
try:
    import websockets  # type: ignore
except ImportError:  # pragma: no cover - runtime env must install websockets
    websockets = None

from bots.models import ParticipantEventTypes


def iter_annexb_nals(bs: bytes):
    n = len(bs)

    def _next_start(i0):
        i = i0
        while i + 3 < n:
            if bs[i] == 0 and bs[i + 1] == 0 and bs[i + 2] == 1:
                return i
            if i + 4 < n and bs[i] == 0 and bs[i + 1] == 0 and bs[i + 2] == 0 and bs[i + 3] == 1:
                return i
            i += 1
        return -1

    start = _next_start(0)
    while start != -1:
        next_start = _next_start(start + 3)
        nal = bs[start : next_start if next_start != -1 else n]
        # nal header is after the start code
        hdr_idx = start + (4 if bs[start + 2] == 0 else 3)
        if hdr_idx < len(bs):
            nal_type = bs[hdr_idx] & 0x1F
            yield nal, nal_type
        start = next_start


def is_keyframe(bs: bytes) -> bool:
    """Return True if this buffer contains SPS(7)/PPS(8) and an IDR(5)."""
    saw_sps = saw_pps = saw_idr = False
    for _, t in iter_annexb_nals(bs):
        if t == 7:
            saw_sps = True
        elif t == 8:
            saw_pps = True
        elif t == 5:
            saw_idr = True
    # IDR is the key; SPS/PPS often precede it (parser can also inject), but prefer all three.
    return saw_idr or (saw_sps and saw_pps)


def make_black_h264_annexb(width: int, height: int, fps=(30, 1)) -> bytes:
    """
    One *AU-aligned* Annex-B black frame (AUD+SPS+PPS+IDR) that matches:
        video/x-h264,stream-format=byte-stream,alignment=au
    """
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)

    pipe = Gst.parse_launch(
        f"videotestsrc pattern=black is-live=false num-buffers=1 ! "
        f"video/x-raw,format=I420,width={width},height={height},"
        f"framerate={fps[0]}/{fps[1]} ! "
        # x264 in Annex-B
        "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=1 bframes=0 "
        "byte-stream=true aud=true ! "
        # parse + FORCE downstream caps to Annex-B + AU alignment
        "h264parse disable-passthrough=true ! "
        "video/x-h264,stream-format=byte-stream,alignment=au ! "
        "appsink name=bsink emit-signals=true sync=false max-buffers=1 drop=false"
    )

    sink = pipe.get_by_name("bsink")
    pipe.set_state(Gst.State.PLAYING)
    sample = sink.emit("pull-sample")  # our single AU
    buf = sample.get_buffer()
    data = buf.extract_dup(0, buf.get_size())
    pipe.set_state(Gst.State.NULL)

    # sanity: Annex-B should start with 00 00 00 01
    assert data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01")
    return data


def generate_signature(client_id: str, meeting_uuid: str, stream_id: str, client_secret: str) -> str:
    """
    Generate signature for RTMS authentication.

    Matches your sample Python RTMS code:
        message = f"{client_id},{meeting_uuid},{stream_id}"
    """
    message = f"{client_id},{meeting_uuid},{stream_id}"
    return hmac.new(
        client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def extract_join_info(join_payload: dict):
    """
    Tolerant extraction of (meeting_uuid, rtms_stream_id, signaling_url) from the join payload.

    join_payload might be:
      - the 'payload' field of the webhook, or
      - the entire webhook envelope.

    Expected keys (case-insensitive-ish):
      - meeting_uuid / meetingUuid
      - rtms_stream_id / rtmsStreamId
      - server_urls / serverUrls / server_url
    """
    payload = join_payload.get("payload", join_payload)

    meeting_uuid = payload.get("meeting_uuid") or payload.get("meetingUuid")
    stream_id = payload.get("rtms_stream_id") or payload.get("rtmsStreamId")
    server_urls = payload.get("server_urls") or payload.get("serverUrls") or payload.get("server_url")

    if isinstance(server_urls, dict):
        # If Zoom ever returns a dict of URLs, pick reasonable default.
        signaling_url = server_urls.get("signaling") or server_urls.get("all") or next(iter(server_urls.values()), None)
    else:
        signaling_url = server_urls

    if not (meeting_uuid and stream_id and signaling_url):
        raise ValueError(f"join_payload missing required fields. meeting_uuid={meeting_uuid}, rtms_stream_id={stream_id}, server_urls={server_urls}")

    return meeting_uuid, stream_id, signaling_url


class RTMSClient:
    """
    A pure-Python RTMS client roughly equivalent to the Node rtms.Client usage.

    It:
      * connects to signaling + media WebSockets using your sample RTMS protocol
      * does the handshakes (msg_type 1/2 for signaling, 3/4 for media)
      * sends keep-alive responses (12/13)
      * handles MEDIA_DATA_AUDIO (14), MEDIA_DATA_VIDEO (15, assumed), MEDIA_DATA_TRANSCRIPT (17, assumed)
      * calls into ZoomRTMSAdapter for:
          - _on_audio_frame(frame, user_name, user_id)
          - _on_video_frame(frame, user_name, user_id)
          - post_rtms_event({...})   # for transcriptUpdate / firstVideoFrameReceived, etc.
    """

    MEDIA_TYPE_AUDIO = 1
    MEDIA_TYPE_VIDEO = 2
    MEDIA_TYPE_TRANSCRIPT = 8  # from Zoom docs samples

    def __init__(
        self,
        *,
        zoom_client_id: str,
        zoom_client_secret: str,
        join_payload: dict,
        use_audio: bool,
        use_video: bool,
        use_transcript: bool,
        adapter: "ZoomRTMSAdapter",
    ):
        if websockets is None:
            raise RuntimeError("The 'websockets' package is required to use RTMSClient.")

        self.zoom_client_id = zoom_client_id
        self.zoom_client_secret = zoom_client_secret
        self.join_payload = join_payload

        self.use_audio = use_audio
        self.use_video = use_video
        self.use_transcript = use_transcript

        self.adapter = adapter

        self.meeting_uuid, self.stream_id, self.signaling_url = extract_join_info(join_payload)

        self.signaling_ws = None
        self.media_ws = None

        self._closing = asyncio.Event()
        self._loop = None

        self._first_video_frame_reported = False

    async def run(self) -> None:
        """Entry point for the RTMS client. Intended to be run inside asyncio.run(...) in a dedicated thread."""
        self._loop = asyncio.get_running_loop()
        logger.info(
            "RTMSClient.run starting for meeting_uuid=%s, stream_id=%s, signaling_url=%s",
            self.meeting_uuid,
            self.stream_id,
            self.signaling_url,
        )
        try:
            await self._connect_signaling()
        finally:
            logger.info("RTMSClient.run exiting")
            self._closing.set()
            self.adapter.handle_rtms_exit()

    def request_shutdown(self) -> None:
        """
        Thread-safe method to ask the RTMS client to shut down.

        Called from ZoomRTMSAdapter.cleanup() on the GLib thread.
        If the asyncio loop is already closed (because asyncio.run()
        has finished), there is nothing to do.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug("RTMSClient.request_shutdown: loop is None or already closed; nothing to do")
            return

        def _shutdown():
            if self._closing.is_set():
                return

            self._closing.set()

            async def _close_all():
                try:
                    if self.media_ws is not None:
                        await self.media_ws.close()
                except Exception:
                    logger.exception("Error closing media websocket")

                try:
                    if self.signaling_ws is not None:
                        await self.signaling_ws.close()
                except Exception:
                    logger.exception("Error closing signaling websocket")

            asyncio.create_task(_close_all())

        try:
            loop.call_soon_threadsafe(_shutdown)
        except RuntimeError as e:
            # Small race: loop might have closed after is_closed() check
            if "Event loop is closed" in str(e):
                logger.debug("RTMSClient.request_shutdown: loop closed during call_soon_threadsafe; ignoring")
            else:
                raise

    # ---- internal helpers -------------------------------------------------

    def _build_ssl_context(self, url: str):
        if url.startswith("wss://"):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # You may want to change this in production:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    async def _connect_signaling(self) -> None:
        ssl_context = self._build_ssl_context(self.signaling_url)

        try:
            async with websockets.connect(self.signaling_url, ssl=ssl_context) as ws:
                self.signaling_ws = ws
                logger.info("Signaling WebSocket connected to %s", self.signaling_url)

                signature = generate_signature(
                    self.zoom_client_id,
                    self.meeting_uuid,
                    self.stream_id,
                    self.zoom_client_secret,
                )

                handshake = {
                    "msg_type": 1,  # SIGNALING_HAND_SHAKE_REQ
                    "protocol_version": 1,
                    "meeting_uuid": self.meeting_uuid,
                    "rtms_stream_id": self.stream_id,
                    "sequence": int(asyncio.get_event_loop().time() * 1e9),
                    "signature": signature,
                }
                await ws.send(json.dumps(handshake))
                logger.info("Sent signaling handshake")

                async for raw in ws:
                    if self._closing.is_set():
                        break

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Non-JSON signaling message: %r", raw)
                        continue

                    msg_type = msg.get("msg_type")
                    logger.info("Signaling message: %s", msg)

                    # Handshake response
                    if msg_type == 2 and msg.get("status_code") == 0:  # SIGNALING_HAND_SHAKE_RESP
                        media_server = msg.get("media_server", {})
                        server_urls = media_server.get("server_urls", {})
                        media_url = server_urls.get("all") or server_urls.get("audio") or server_urls.get("video")

                        # Subscribe to in-meeting events on the signaling socket
                        try:
                            await self._subscribe_in_meeting_events()
                        except Exception:
                            logger.exception("Error subscribing to in-meeting events")

                        # Then connect to the media websocket (as before)
                        if media_url:
                            logger.info("Connecting to media WebSocket at %s", media_url)
                            asyncio.create_task(self._connect_media(media_url))
                        else:
                            logger.warning("No media_url found in signaling handshake response: %s", msg)

                    # Keep-alive
                    elif msg_type == 12:  # KEEP_ALIVE_REQ
                        resp = {
                            "msg_type": 13,  # KEEP_ALIVE_RESP
                            "timestamp": msg.get("timestamp"),
                        }
                        logger.debug("Responding to signaling KEEP_ALIVE_REQ: %s", resp)
                        await ws.send(json.dumps(resp))

                    # Active speaker change
                    elif msg_type == 6 and msg.get("event", {}).get("event_type") == 2:
                        await self._handle_active_speaker_change(msg)

                    # Participant join / leave
                    elif msg_type == 6 and (msg.get("event", {}).get("event_type") == 3 or msg.get("event", {}).get("event_type") == 4):
                        await self._handle_participant_join_or_leave(msg)

                    # Stream state update
                    elif msg_type == 8:
                        await self._handle_stream_state_update(msg)

                    # Session state update
                    elif msg_type == 9:
                        await self._handle_session_state_update(msg)

        except Exception:
            logger.exception("Signaling socket error")
        finally:
            logger.info("Signaling socket closed")

    async def _subscribe_in_meeting_events(self) -> None:
        """
        Subscribe to in-meeting events on the signaling connection:

          - event_type 2: active speaker change
          - event_type 3: participant join
          - event_type 4: participant leave
        """
        if self.signaling_ws is None:
            logger.warning("Cannot subscribe to in-meeting events: signaling_ws is None")
            return

        sub_msg = {
            "msg_type": 5,
            "events": [
                {"event_type": 2, "subscribe": True},  # active speaker change
                {"event_type": 3, "subscribe": True},  # participant join
                {"event_type": 4, "subscribe": True},  # participant leave
            ],
        }

        try:
            await self.signaling_ws.send(json.dumps(sub_msg))
            logger.info("Subscribed to in-meeting events (event_type 2/3/4: active speaker / join / leave)")
        except Exception:
            logger.exception("Failed to subscribe to in-meeting events")

    async def _connect_media(self, media_url: str) -> None:
        if self._closing.is_set():
            return

        ssl_context = self._build_ssl_context(media_url)

        try:
            async with websockets.connect(media_url, ssl=ssl_context) as ws:
                self.media_ws = ws
                logger.info("Media WebSocket connected to %s", media_url)

                signature = generate_signature(
                    self.zoom_client_id,
                    self.meeting_uuid,
                    self.stream_id,
                    self.zoom_client_secret,
                )

                # ---------------------------
                # IMPORTANT: media_type
                # ---------------------------
                # Match your working JS client:
                #
                #  - audio-only: 1
                #  - audio+video+transcript: 32
                #
                if self.use_video or self.use_transcript:
                    media_type = 32  # AUDIO+VIDEO+TRANSCRIPT (as in JS example)
                else:
                    media_type = 1  # AUDIO only

                handshake = {
                    "msg_type": 3,  # DATA_HAND_SHAKE_REQ
                    "protocol_version": 1,
                    "meeting_uuid": self.meeting_uuid,
                    "rtms_stream_id": self.stream_id,
                    "signature": signature,
                    "media_type": media_type,
                    "payload_encryption": False,
                }

                # When we request video (or transcript), include media_params just
                # like your working JS example does.
                if media_type == 32:
                    if self.adapter.video_frame_size == (1280, 720):
                        video_resolution_for_media_params = 2
                    elif self.adapter.video_frame_size == (1920, 1080):
                        video_resolution_for_media_params = 3
                    else:
                        raise ValueError(f"Unsupported video frame size: {self.adapter.video_frame_size}")

                    handshake["media_params"] = {
                        "audio": {
                            "content_type": 1,
                            "sample_rate": 1,
                            "channel": 1,
                            "codec": 1,
                            "data_opt": 1,
                            "send_rate": 100,
                        },
                        "video": {
                            "codec": 7,  # H264
                            "resolution": video_resolution_for_media_params,  # HD
                            "fps": 15,
                        },
                    }

                logger.info("Sending media handshake: %s", handshake)
                await ws.send(json.dumps(handshake))

                async for raw in ws:
                    if self._closing.is_set():
                        break

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Non-JSON media message (possibly binary): %r", raw)
                        continue

                    msg_type = msg.get("msg_type")
                    content = msg.get("content", {})
                    logger.debug("Media message: %s", msg)

                    # Media handshake response
                    if msg_type == 4 and msg.get("status_code") == 0:  # DATA_HAND_SHAKE_RESP
                        logger.info("Media handshake successful")
                        # Tell signaling we are ready to receive data
                        if self.signaling_ws is not None:
                            ack = {
                                "msg_type": 7,  # CLIENT_READY_ACK
                                "rtms_stream_id": self.stream_id,
                            }
                            try:
                                await self.signaling_ws.send(json.dumps(ack))
                                logger.info("Sent CLIENT_READY_ACK to signaling server")
                            except Exception:
                                logger.exception("Failed to send CLIENT_READY_ACK to signaling server")

                    # Keep-alive
                    elif msg_type == 12:  # KEEP_ALIVE_REQ
                        resp = {
                            "msg_type": 13,  # KEEP_ALIVE_RESP
                            "timestamp": msg.get("timestamp"),
                        }
                        logger.info("Responding to media KEEP_ALIVE_REQ: %s", resp)
                        await ws.send(json.dumps(resp))

                    # Audio data
                    elif msg_type == 14 and self.use_audio:  # MEDIA_DATA_AUDIO
                        await self._handle_audio(content)

                    # Video data (15 in your JS example)
                    elif msg_type == 15 and self.use_video:  # MEDIA_DATA_VIDEO
                        await self._handle_video(content)

                    # Transcript data (17 in your JS example)
                    elif msg_type == 17 and self.use_transcript:  # MEDIA_DATA_TRANSCRIPT
                        self._handle_transcript(content)

                    else:
                        # other media messages ignored for now
                        pass

        except Exception:
            logger.exception("Media socket error")
            self.adapter.handle_fatal_media_socket_error()
        finally:
            logger.info("Media socket closed")

    async def _handle_participant_join_or_leave(self, content: dict) -> None:
        for participant in content.get("event", {}).get("participants", []):
            event = {
                "type": "userUpdate",
                "user": {
                    "id": participant.get("user_id"),
                    "name": participant.get("user_name"),
                },
                "join": content.get("event", {}).get("event_type") == 3,
            }
            self.adapter.post_rtms_event(event)

    async def _handle_active_speaker_change(self, content: dict) -> None:
        user_id = content.get("event", {}).get("user_id")
        user_name = content.get("event", {}).get("user_name")
        event = {
            "type": "activeSpeakerChange",
            "user_id": user_id,
            "user_name": user_name,
        }
        self.adapter.post_rtms_event(event)

    async def _handle_stream_state_update(self, content: dict) -> None:
        state = content.get("state")
        event = {
            "type": "streamUpdate",
            "state": state,
        }
        self.adapter.post_rtms_event(event)

    async def _handle_session_state_update(self, content: dict) -> None:
        state = content.get("state")
        event = {
            "type": "sessionUpdate",
            "state": state,
        }
        self.adapter.post_rtms_event(event)

    async def _handle_audio(self, content: dict) -> None:
        data_b64 = content.get("data")
        if not data_b64:
            return

        try:
            frame = base64.b64decode(data_b64)
        except Exception:
            logger.exception("Error base64-decoding audio frame")
            return

        user_id = content.get("user_id")
        user_name = content.get("user_name")

        self.adapter._on_audio_frame(frame, user_name, user_id)

    async def _handle_video(self, content: dict) -> None:
        data_b64 = content.get("data")
        if not data_b64:
            return

        try:
            frame = base64.b64decode(data_b64)
        except Exception:
            logger.exception("Error base64-decoding video frame")
            return

        user_id = content.get("user_id") or content.get("userId") or -1
        user_name = content.get("user_name") or content.get("userName") or ""

        self.adapter._on_video_frame(frame, user_name, int(user_id))

        if not self._first_video_frame_reported:
            self._first_video_frame_reported = True
            # Mirror the Node client: emit firstVideoFrameReceived via the JSON path
            self.adapter.post_rtms_event({"type": "firstVideoFrameReceived"})

    def _handle_transcript(self, content: dict) -> None:
        """
        MEDIA_DATA_TRANSCRIPT handler.

        Expected shape (inferred; adjust to actual RTMS doc):
          {
            "data": "Hello world",
            "user_id": 123,
            "user_name": "Alice",
            "caption_id": "abc123"  # optional
          }
        """
        logger.info("RTMS transcriptUpdate RAW: %s", content)
        text = content.get("data", "")
        user_id = content.get("user_id") or content.get("userId")
        user_name = content.get("user_name") or content.get("userName")
        caption_id = str(user_id) + "." + str(content.get("timestamp"))  # Timestamp + user id should uniquely identify a caption

        event = {
            "type": "transcriptUpdate",
            "user": {
                "id": user_id,
                "name": user_name,
            },
            "text": text,
            "caption_id": caption_id,
        }
        self.adapter.post_rtms_event(event)


class ZoomRTMSAdapter(BotAdapter):
    def __init__(
        self,
        *,
        use_one_way_audio,
        use_mixed_audio,
        use_video,
        send_message_callback,
        add_audio_chunk_callback,
        zoom_rtms,
        add_video_frame_callback,
        wants_any_video_frames_callback,
        add_mixed_audio_chunk_callback,
        zoom_client_id,
        zoom_client_secret,
        upsert_chat_message_callback,
        upsert_caption_callback,
        add_participant_event_callback,
        video_frame_size: tuple[int, int],
    ):
        self.zoom_rtms = zoom_rtms
        self.use_one_way_audio = use_one_way_audio
        self.use_mixed_audio = use_mixed_audio
        self.use_video = use_video
        self.send_message_callback = send_message_callback
        self.add_audio_chunk_callback = add_audio_chunk_callback
        self.add_mixed_audio_chunk_callback = add_mixed_audio_chunk_callback
        self.add_video_frame_callback = add_video_frame_callback
        self.wants_any_video_frames_callback = wants_any_video_frames_callback

        self.zoom_client_id = zoom_client_id
        self.zoom_client_secret = zoom_client_secret

        self.video_frame_size = video_frame_size

        self.last_audio_received_at = None
        self.last_video_received_at = None
        self.cleaned_up = False
        self.left_meeting = False

        self.active_speaker_id = None
        self.active_speaker_name = None

        self._participant_cache = {}

        self.upsert_chat_message_callback = upsert_chat_message_callback
        self.add_participant_event_callback = add_participant_event_callback
        self.upsert_caption_callback = upsert_caption_callback

        self.first_buffer_timestamp_ms = None

        self.last_audio_frame_speaker_name = None
        self.black_frame = make_black_h264_annexb(self.video_frame_size[0], self.video_frame_size[1])

        self.black_frame_timer_id = None
        self.connected_at = None
        self.waiting_for_keyframe = False
        self.last_keyframe_received_at = time.time()
        self.rtms_paused = False

        # Pure-Python RTMS client + thread
        self._rtms_client: RTMSClient | None = None
        self._rtms_thread: threading.Thread | None = None

    # --------------------------------------------------------------------- RTMS

    def send_black_frame(self):
        current_time = time.time()
        if self.connected_at is not None and current_time - self.connected_at >= 1.0:
            if self.last_video_received_at is None or current_time - self.last_video_received_at >= 0.25:
                # Create a black frame of the same dimensions
                if self.rtms_paused:
                    name_to_render = "Paused"
                else:
                    name_to_render = self.active_speaker_name or ""

                self._on_video_frame(self.black_frame, name_to_render, -1)
                logger.info("Sent black frame for name: %s", name_to_render)

        # Keep the GLib timeout active until we've been cleaned up
        return not self.cleaned_up

    def _on_audio_frame(self, frame: bytes, userName: str, userId: int):
        """
        Called for each Opus audio frame (mixed, 16kHz mono).
        """
        self.last_audio_received_at = time.time()
        if not self.rtms_paused:
            self.last_audio_frame_speaker_name = userName
        try:
            if self.use_mixed_audio and self.add_mixed_audio_chunk_callback:
                self.add_mixed_audio_chunk_callback(frame)
            if self.use_one_way_audio and self.add_audio_chunk_callback:
                current_time = datetime.utcnow()
                userIdToSend = userId or self.active_speaker_id
                if userIdToSend is not None:
                    self.add_audio_chunk_callback(userIdToSend, current_time, frame)

        except Exception:
            logger.exception("Audio frame handling failed")

    def _on_video_frame(self, frame: bytes, userName: str, userId: int):
        """
        Called for each H.264 frame with username and user ID.
        """
        if frame != self.black_frame:
            if is_keyframe(frame):
                self.waiting_for_keyframe = False
                self.last_keyframe_received_at = time.time()
                logger.info("Received keyframe")
            else:
                if self.waiting_for_keyframe:
                    logger.info("Received video frame but not a keyframe. Waiting for keyframe...")
                    return

        self.last_video_received_at = time.time()
        try:
            if self.wants_any_video_frames_callback and not self.wants_any_video_frames_callback():
                return
            if self.add_video_frame_callback:
                self.add_video_frame_callback(
                    frame,
                    time.time_ns(),
                    overlay_text=userName or self.active_speaker_name or "",
                )
        except Exception:
            logger.exception("Video frame handling failed")

    def post_rtms_event(self, event: dict):
        """
        Schedule handling of an RTMS "JSON event" (userUpdate, transcriptUpdate, firstVideoFrameReceived, sessionUpdate)
        on the GLib main thread, matching the previous subprocess/stdout behavior.
        """

        json_str = json.dumps(event)

        def _run():
            try:
                self.handle_rtms_json_message(json_str)
            except Exception:
                logger.exception("Error handling RTMS event")
            return False  # run once

        GLib.idle_add(_run)

    def cleanup(self):
        logger.info("cleanup called")
        self.cleaned_up = True

        if self.black_frame_timer_id is not None:
            GLib.source_remove(self.black_frame_timer_id)
            self.black_frame_timer_id = None

        # Shut down RTMS client + thread
        if self._rtms_client is not None:
            self._rtms_client.request_shutdown()
        if self._rtms_thread is not None and self._rtms_thread.is_alive():
            self._rtms_thread.join(timeout=5.0)
            self._rtms_thread = None

    def init(self):
        logger.info("init called")
        self.initialize_rtms_connection()
        return

    def initialize_rtms_connection(self):
        logger.info("Initializing RTMS connection...")

        need_audio = self.use_one_way_audio or self.use_mixed_audio
        need_video = self.use_video

        try:
            self._rtms_client = RTMSClient(
                zoom_client_id=self.zoom_client_id,
                zoom_client_secret=self.zoom_client_secret,
                join_payload=self.zoom_rtms,
                use_audio=need_audio,
                use_video=need_video,
                # Only subscribe to transcript if we are NOT doing our own audio transcription
                use_transcript=self.add_audio_chunk_callback is None,
                adapter=self,
            )

            def _run_client():
                try:
                    asyncio.run(self._rtms_client.run())
                except Exception:
                    logger.exception("RTMS client thread crashed")

            self._rtms_thread = threading.Thread(
                target=_run_client,
                daemon=True,
                name="ZoomRTMSClientThread",
            )
            self._rtms_thread.start()

            # Start black-frame timer as before
            self.black_frame_timer_id = GLib.timeout_add(250, self.send_black_frame)
            self.connected_at = time.time()

            logger.info("RTMS client started successfully (in-process)")
            self.send_message_callback({"message": self.Messages.APP_SESSION_CONNECTED})

        except Exception:
            logger.exception("Failed to start RTMS client")

        return

    # ------------------------------------------------------------------ events

    def get_participant(self, participant_id):
        return self._participant_cache.get(participant_id)

    def handle_fatal_media_socket_error(self):
        logger.info("handle_fatal_media_socket_error called")
        self.send_message_callback({"message": self.Messages.APP_SESSION_DISCONNECT_REQUESTED})

    def handle_rtms_exit(self):
        if self.left_meeting:
            return
        if self.cleaned_up:
            return
        logger.info("handle_rtms_exit called")
        self.send_message_callback({"message": self.Messages.APP_SESSION_DISCONNECT_REQUESTED})

    def handle_rtms_json_message(self, json_data):
        logger.info("handle_rtms_json_message called with json_data: %s", json_data)
        json_data = json.loads(json_data)
        if json_data.get("type") == "userUpdate":
            logger.info("RTMS userUpdate: %s", json_data)
            # {'op': 0, 'user': {'id': 16778240, 'name': 'Noah Duncan'}, 'type': 'userUpdate'}
            user_id = json_data.get("user").get("id")
            user_name = json_data.get("user").get("name") or self._participant_cache.get(user_id, {}).get("participant_full_name")

            self._participant_cache[user_id] = {
                "participant_uuid": user_id,
                "participant_user_uuid": None,
                "participant_full_name": user_name,
                "participant_is_the_bot": False,
                "participant_is_host": False,
            }

            self.add_participant_event_callback(
                {
                    "participant_uuid": user_id,
                    "event_type": ParticipantEventTypes.JOIN if json_data.get("join") else ParticipantEventTypes.LEAVE,
                    "event_data": {},
                    "timestamp_ms": int(time.time() * 1000),
                }
            )
        elif json_data.get("type") == "transcriptUpdate":
            # Don't need captions if we're transcribing from audio
            if self.add_audio_chunk_callback:
                return

            logger.info("RTMS transcriptUpdate: %s", json_data)
            # {'user': {'userId': 16778240, 'name': 'Noah Duncan'},
            #  'text': 'Hello, how are you?', 'type': 'transcriptUpdate'}

            device_id = json_data.get("user").get("id")
            caption_id = json_data.get("caption_id")

            itemConverted = {
                "deviceId": device_id,
                "captionId": caption_id,
                "text": json_data.get("text"),
                "isFinal": True,
            }

            self.upsert_caption_callback(itemConverted)

        elif json_data.get("type") == "firstVideoFrameReceived":
            self.first_buffer_timestamp_ms = time.time() * 1000

        elif json_data.get("type") == "sessionUpdate":
            state = json_data.get("state")
            # This means it was paused
            if state == 3:
                logger.info("RTMS sessionUpdate: Paused")
                self.last_audio_frame_speaker_name = "Paused"
                self.rtms_paused = True
            # This means it was resumed
            if state == 4:
                logger.info("RTMS sessionUpdate: Resumed")
                self.last_audio_frame_speaker_name = None
                self.waiting_for_keyframe = True
                self.rtms_paused = False

        elif json_data.get("type") == "activeSpeakerChange":
            user_id = json_data.get("user_id")
            user_name = json_data.get("user_name")
            self.active_speaker_id = user_id
            self.active_speaker_name = user_name
            logger.info("RTMS activeSpeakerChange: %s", json_data)

        elif json_data.get("type") == "streamUpdate":
            state = json_data.get("state")
            if state == 4:
                logger.info("RTMS streamUpdate: Ended")
                self.send_message_callback({"message": self.Messages.APP_SESSION_DISCONNECT_REQUESTED})

    # ------------------------------------------------------------- control API

    def send_raw_image(self, png_image_bytes):
        # Not currently supported for Zoom RTMS receive-only mode
        return

    def send_raw_audio(self, bytes_, sample_rate):
        # Not currently supported for Zoom RTMS receive-only mode
        return

    def disconnect(self):
        if self.left_meeting:
            return
        logger.info("disconnect called")

        self.cleanup()

        self.left_meeting = True
        self.send_message_callback({"message": self.Messages.APP_SESSION_DISCONNECTED})
        return

    def leave(self):
        return self.disconnect()

    def get_first_buffer_timestamp_ms(self):
        return self.first_buffer_timestamp_ms

    def check_auto_leave_conditions(self):
        return

    def is_sent_video_still_playing(self):
        return False

    def send_video(self, video_url, loop=False):
        logger.info(f"send_video called with video_url = {video_url}, loop = {loop}. This is not supported for Zoom RTMS adapter")
        return

    def get_first_buffer_timestamp_ms_offset(self):
        return 0
