import logging

from selenium import webdriver

logger = logging.getLogger(__name__)

import asyncio
import os
import time

import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRelay
from av.audio.frame import AudioFrame
from pyvirtualdisplay import Display

os.environ["PULSE_LATENCY_MSEC"] = "20"


class LatestFrameTrack(VideoStreamTrack):
    """
    Wraps another VideoStreamTrack and always exposes the latest frame,
    dropping older frames if we fall behind.

    This gives "live-ish" behavior instead of slowly draining a backlog.
    """

    def __init__(self, source_track: VideoStreamTrack):
        super().__init__()
        self._source = source_track
        self._queue = asyncio.Queue(maxsize=1)
        self._reader_task = asyncio.create_task(self._reader())
        self._stopped = False

    async def _reader(self):
        try:
            while not self._stopped:
                frame = await self._source.recv()

                # Keep only the freshest frame in our queue
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass

                await self._queue.put(frame)
        except Exception:
            # Source ended or PC closed; just exit
            pass

    async def recv(self):
        # Wait for the next frame that the reader has pulled
        frame = await self._queue.get()

        # Re-stamp timing using our own clock so aiortc sees this as real-time
        pts, time_base = await self.next_timestamp()
        frame.pts = pts
        frame.time_base = time_base
        return frame

    async def stop(self):
        self._stopped = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except Exception:
                pass
        await super().stop()


def audioframe_to_s16le_bytes(frame: AudioFrame, target_channels=2):
    """
    Convert aiortc AudioFrame to interleaved s16le bytes at 48k stereo.
    """
    # Ensure 48k
    if frame.sample_rate != 48000:
        # aiortc typically gives 48k; if not, let av resample:
        frame.pts = None
        frame.sample_rate = 48000

    # Convert to s16
    pcm = frame.to_ndarray(format="s16")  # shape: (channels, samples)
    if pcm.ndim == 1:
        pcm = np.expand_dims(pcm, axis=0)

    # Upmix/downmix to target_channels
    ch = pcm.shape[0]
    if ch < target_channels:
        pcm = np.vstack([pcm] + [pcm[0:1, :]] * (target_channels - ch))
    elif ch > target_channels:
        pcm = pcm[:target_channels, :]

    # Interleave channels (C, N) -> (N, C) -> bytes
    interleaved = pcm.T.astype(np.int16).tobytes()
    return interleaved


class AlsaLoopbackSink:
    """
    Writes 48k s16le stereo PCM to ALSA loopback using ffmpeg.
    Target device: hw:Loopback,0,0 (provided by snd-aloop).
    """

    def __init__(self, device="hw:Loopback,0,0", sample_rate=48000, channels=2):
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self._proc = None
        self._stdin = None
        self._task = None
        self._stopped = asyncio.Event()

    def start(self):
        # Build ffmpeg proc: raw s16le → ALSA device
        pass

    def write(self, pcm_bytes: bytes):
        # Calculate volume (RMS) of the PCM data
        if len(pcm_bytes) > 0:
            # Convert bytes back to int16 array for volume calculation
            pcm_array = np.frombuffer(pcm_bytes, dtype=np.int16)
            # Calculate RMS (Root Mean Square) for volume
            rms = np.sqrt(np.mean(pcm_array.astype(np.float32) ** 2))
            # Normalize to 0-100 scale (int16 max is 32767)
            volume_percent = (rms / 32767.0) * 100
            print(f"PCM volume: {volume_percent:.2f}% (RMS: {rms:.1f})")
        else:
            print("Empty PCM data")

        print(f"PCM bytes length: {len(pcm_bytes)}")

    async def stop(self):
        pass


class WebpageStreamer:
    def __init__(
        self,
        video_frame_size,
    ):
        self.driver = None
        self.video_frame_size = video_frame_size
        self.display_var_for_recording = None
        self.display = None
        self.last_keepalive_time = None
        self.web_app = None

    def run(self):
        self.display_var_for_recording = os.environ.get("DISPLAY")
        if os.environ.get("DISPLAY") is None:
            # Create virtual display only if no real display is available
            self.display = Display(visible=0, size=self.video_frame_size)
            self.display.start()
            self.display_var_for_recording = self.display.new_display_var

        options = webdriver.ChromeOptions()

        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--use-fake-device-for-media-stream")
        # options.add_argument("--use-fake-ui-for-media-stream")
        options.add_argument(f"--window-size={self.video_frame_size[0]},{self.video_frame_size[1]}")
        options.add_argument("--start-fullscreen")

        # options.add_argument('--headless=new')
        options.add_argument("--disable-gpu")
        # options.add_argument("--mute-audio")
        options.add_argument("--disable-application-cache")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--enable-blink-features=WebCodecs,WebRTC-InsertableStreams,-AutomationControlled")
        options.add_argument("--remote-debugging-port=9222")

        if os.getenv("ENABLE_CHROME_SANDBOX_FOR_WEBPAGE_STREAMER", "true").lower() != "true":
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-setuid-sandbox")
            logger.info("Chrome sandboxing is disabled")
        else:
            logger.info("Chrome sandboxing is enabled")
        logger.info(f"Video frame size: {self.video_frame_size}")

        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        options.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.media_stream_mic": 1,  # 1 = allow, 2 = block
                "profile.default_content_setting_values.media_stream_camera": 2,  # 1 = allow, 2 = block
            },
        )

        self.driver = webdriver.Chrome(options=options)
        logger.info(f"web driver server initialized at port {self.driver.service.port}")

        with open("bots/webpage_streamer/webpage_streamer_payload.js", "r") as file:
            payload_code = file.read()

        combined_code = f"""
            {payload_code}
        """

        # Add the combined script to execute on new document
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": combined_code})

        self.load_webapp()

    async def keepalive_monitor(self):
        """Monitor keepalive status and shutdown if no keepalive received in the last 15 minutes."""

        self.last_keepalive_time = time.time()

        while True:
            await asyncio.sleep(60)  # Check every minute

            current_time = time.time()
            time_since_last_keepalive = current_time - self.last_keepalive_time

            if time_since_last_keepalive > 900:  # More than 15 minutes since last keepalive
                logger.warning(f"No keepalive received in {time_since_last_keepalive:.1f} seconds. Shutting down process.")
                await self.shutdown_process()
                break

    async def shutdown_process(self):
        """Gracefully shutdown the process."""
        try:
            if self.driver:
                self.driver.quit()
            if self.display:
                self.display.stop()
            if self.web_app:
                await self.web_app.shutdown()
            logger.info("Process shutting down")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        finally:
            os._exit(0)

    def load_webapp(self):
        pcs = set()

        # ADD THESE TWO LINES
        AUDIO_RELAY = MediaRelay()
        # will hold the *original* upstream AudioStreamTrack
        # from the first client that posts to /offer
        UPSTREAM_AUDIO_TRACK_KEY = "upstream_audio_track"

        async def offer_meeting_audio(req):
            """
            POST /offer_meeting_audio
            Return an SDP answer that *sends* the upstream audio (if present)
            to this new peer connection (listen-only client).
            """
            params = await req.json()
            offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

            # Do we have an upstream audio yet?
            upstream = req.app.get(UPSTREAM_AUDIO_TRACK_KEY)
            if upstream is None:
                return web.Response(status=409, text="No upstream audio has been published yet.")

            pc = RTCPeerConnection()
            pcs.add(pc)

            # Re-broadcast using the relay so multiple listeners are OK
            rebroadcast_track = AUDIO_RELAY.subscribe(upstream)
            pc.addTrack(rebroadcast_track)

            @pc.on("connectionstatechange")
            async def _on_state():
                if pc.connectionState in ("failed", "closed", "disconnected"):
                    await pc.close()
                    pcs.discard(pc)

            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

        async def offer(req):
            params = await req.json()
            offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

            pc = RTCPeerConnection()
            pcs.add(pc)

            # --- server-to-client (unchanged): send your local video/audio if desired ---
            v_player = req.app["video_player"]
            a_player = req.app["audio_player"]

            if v_player and v_player.video:
                latest_video_track = LatestFrameTrack(v_player.video)
                v_sender = pc.addTrack(latest_video_track)
                # Hint the encoder for real-time, modest bitrate, no B-frames
                try:
                    params = v_sender.getParameters()
                    # Keep one encoding, cap bitrate to avoid queue build-up
                    params.encodings = [{"maxBitrate": 750000, "maxFramerate": 15}]
                    v_sender.setParameters(params)
                except Exception:
                    pass

            # You can still send server audio if you want the page to hear server audio too:
            if a_player and a_player.audio:
                a_sender = pc.addTrack(a_player.audio)
                # Hint the encoder for real-time, modest bitrate, no B-frames
                try:
                    params = a_sender.getParameters()
                    # Keep one encoding, cap bitrate to avoid queue build-up
                    # params.encodings = [{"maxBitrate": 64_000, "maxFramerate": 15}]
                    a_sender.setParameters(params)
                except Exception:
                    pass

            # --- NEW: receive client's mic and feed to ALSA loopback ---
            # loopback_sink = AlsaLoopbackSink(device="hw:Loopback,0,0", sample_rate=48000, channels=2)
            # loopback_sink.start()

            @pc.on("track")
            def on_track(track):
                if track.kind == "audio":
                    # store the ORIGINAL upstream track for rebroadcast
                    req.app[UPSTREAM_AUDIO_TRACK_KEY] = track
                    logger.info("Upstream audio track set for rebroadcast.")

            @pc.on("connectionstatechange")
            async def _on_state():
                if pc.connectionState in ("failed", "closed", "disconnected"):
                    await pc.close()
                    pcs.discard(pc)
                    pass
                    # await loopback_sink.stop()

            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

        async def start_streaming(req):
            data = await req.json()
            webpage_url = data.get("url")
            if not webpage_url:
                return web.json_response({"error": "URL is required"}, status=400)

            print(f"Starting streaming to {webpage_url}")
            self.driver.get(webpage_url)

            return web.json_response({"status": "success"})

        async def keepalive(req):
            """Keepalive endpoint to reset the timeout timer."""
            self.last_keepalive_time = time.time()
            logger.info("Keepalive received")
            return web.json_response({"status": "alive", "timestamp": self.last_keepalive_time})

        async def shutdown(req):
            """Shutdown endpoint to gracefully shutdown the process."""
            logger.info("Shutting down process via API endpoint")
            await self.shutdown_process()
            return web.json_response({"status": "success"})

        video_size = f"{self.video_frame_size[0]}x{self.video_frame_size[1]}"
        framerate = "15"
        video_device = self.display.new_display_var
        video_format = "x11grab"

        audio_device = "default"
        audio_format = "pulse"

        port = 8000

        # Build players
        # Video options: set size + fps (many webcams accept these via v4l2)
        v_opts = {
            "video_size": video_size,
            "framerate": framerate,
            "fflags": "nobuffer",
            "flags": "low_delay",
            "probesize": "32",
            "analyzeduration": "0",
            "thread_queue_size": "4",
            "draw_mouse": "0",
        }
        video_player = MediaPlayer(video_device, format=video_format, options=v_opts)

        # Audio player: let aiortc/ffmpeg handle resampling to 48k
        a_opts = {
            "fflags": "nobuffer",
            "probesize": "32",
            "analyzeduration": "0",
            "thread_queue_size": "64",
        }
        audio_player = MediaPlayer(audio_device, format=audio_format, options=a_opts)

        app = web.Application()
        self.web_app = app

        # Start keepalive monitoring task
        async def init_keepalive_monitor(app):
            """Initialize keepalive monitoring when the app starts"""
            logger.info("Starting keepalive monitoring task")
            asyncio.create_task(self.keepalive_monitor())
            logger.info("Started keepalive monitoring task")

        app.on_startup.append(init_keepalive_monitor)

        # Add CORS handling for preflight requests
        async def handle_cors_preflight(request):
            """Handle CORS preflight requests"""
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "86400",
                }
            )

        # Add CORS headers to all responses
        @web.middleware
        async def add_cors_headers(request, handler):
            """Add CORS headers to all responses"""
            response = await handler(request)
            response.headers.update(
                {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                }
            )
            return response

        app.middlewares.append(add_cors_headers)

        app.router.add_post("/start_streaming", start_streaming)

        app.router.add_post("/keepalive", keepalive)
        app.router.add_options("/keepalive", handle_cors_preflight)

        app.router.add_post("/shutdown", shutdown)
        app.router.add_options("/shutdown", handle_cors_preflight)

        app.router.add_post("/offer", offer)
        app.router.add_options("/offer", handle_cors_preflight)

        app.router.add_post("/offer_meeting_audio", offer_meeting_audio)  # SDP exchange
        app.router.add_options("/offer_meeting_audio", handle_cors_preflight)

        app["video_player"] = video_player
        app["audio_player"] = audio_player

        web.run_app(app, host="0.0.0.0", port=port)
