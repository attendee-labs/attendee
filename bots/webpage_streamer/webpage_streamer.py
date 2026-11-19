import logging

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger(__name__)

import asyncio
import json
import os
import subprocess
import time
from fractions import Fraction

import gi
import numpy as np
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from av import AudioFrame, VideoFrame
from pyvirtualdisplay import Display

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp

Gst.init(None)

os.environ["PULSE_LATENCY_MSEC"] = "20"

OUTPUT_DESTINATIONS = ("webcam", "screenshare")
DEFAULT_OUTPUT_DESTINATION = "webcam"


class GstVideoStreamTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, sink, width, height, framerate=15):
        super().__init__()
        self._sink = sink
        self._width = width
        self._height = height
        self._framerate = framerate
        self._base_pts_ns = None

    def _pull_sample(self):
        return self._sink.emit("pull-sample")

    async def recv(self) -> VideoFrame:
        loop = asyncio.get_running_loop()
        sample = await loop.run_in_executor(None, self._pull_sample)
        if sample is None:
            raise asyncio.CancelledError("Video pipeline ended")

        buffer = sample.get_buffer()
        pts_ns = buffer.pts

        ok, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("Could not map video buffer")

        try:
            data = memoryview(mapinfo.data)
            w, h = self._width, self._height

            # I420 layout: Y (W*H), U (W/2*H/2), V (W/2*H/2)
            y_size = w * h
            uv_size = y_size // 4

            y_plane = data[0:y_size]
            u_plane = data[y_size : y_size + uv_size]
            v_plane = data[y_size + uv_size : y_size + 2 * uv_size]

            frame = VideoFrame(format="yuv420p", width=w, height=h)
            frame.planes[0].update(y_plane)
            frame.planes[1].update(u_plane)
            frame.planes[2].update(v_plane)
        finally:
            buffer.unmap(mapinfo)

        if self._base_pts_ns is None:
            self._base_pts_ns = pts_ns

        rel_ns = pts_ns - self._base_pts_ns

        # Reuse the same μs time base as audio for nice alignment
        frame.time_base = Fraction(1, 1_000_000)
        frame.pts = rel_ns // 1_000

        return frame


class GstAudioStreamTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(
        self,
        sink: GstApp.AppSink,
        sample_rate: int = 16000,
        channels: int = 2,
    ):
        super().__init__()
        self._sink = sink
        self._sample_rate = sample_rate
        self._channels = channels
        self._base_pts_ns = None

    def _pull_sample(self):
        return self._sink.emit("pull-sample")

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_running_loop()
        sample = await loop.run_in_executor(None, self._pull_sample)
        if sample is None:
            raise asyncio.CancelledError("Audio pipeline ended")

        buffer = sample.get_buffer()
        pts_ns = buffer.pts

        ok, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("Could not map audio buffer")

        try:
            data = mapinfo.data
            # S16LE: 2 bytes per sample per channel
            num_samples = len(data) // (2 * self._channels)
            if num_samples <= 0:
                raise RuntimeError("Empty audio buffer")
            pcm = np.frombuffer(data, dtype=np.int16).reshape(num_samples, self._channels)
        finally:
            buffer.unmap(mapinfo)

        layout = "stereo" if self._channels == 2 else "mono"
        frame = AudioFrame(format="s16", layout=layout, samples=num_samples)
        frame.planes[0].update(pcm.tobytes())
        frame.sample_rate = self._sample_rate

        if self._base_pts_ns is None:
            self._base_pts_ns = pts_ns

        rel_ns = pts_ns - self._base_pts_ns

        # Reuse the same μs time base as audio for nice alignment
        frame.time_base = Fraction(1, 1_000_000)
        frame.pts = rel_ns // 1_000

        return frame


def ensure_pulse_null_sink(sink_name):
    sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, check=True).stdout
    existing_sink_names = [line.split("\t")[1] for line in sinks.splitlines() if "\t" in line]
    if sink_name in existing_sink_names:
        return
    subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={sink_name}"], check=True, capture_output=True)
    logger.info(f"Created PulseAudio null sink {sink_name}")


class StreamedBrowser:
    """A Chrome instance with its own virtual display and PulseAudio sink, captured into WebRTC tracks.

    Each instance gets a dedicated null sink so that its capture only contains its own page's audio.
    """

    def __init__(self, output_destination, video_frame_size, remote_debugging_port, use_existing_display):
        self.output_destination = output_destination
        self.video_frame_size = video_frame_size
        self.remote_debugging_port = remote_debugging_port
        self.use_existing_display = use_existing_display
        self.pulse_sink_name = f"attendee_{output_destination}"

        self.driver = None
        self.display = None
        self.display_var = None

        # GStreamer-related
        self._gst_pipeline = None
        self._gst_video_sink = None
        self._gst_audio_sink = None
        self.video_track = None
        self.audio_track = None

    def start(self):
        if self.use_existing_display and os.environ.get("DISPLAY"):
            self.display_var = os.environ.get("DISPLAY")
        else:
            self.display = Display(visible=0, size=self.video_frame_size, manage_global_env=False)
            self.display.start()
            self.display_var = self.display.new_display_var

        ensure_pulse_null_sink(self.pulse_sink_name)

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
        options.add_argument(f"--remote-debugging-port={self.remote_debugging_port}")

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

        # Chrome inherits chromedriver's environment, which is how it picks up its display and audio sink
        chrome_env = {**os.environ, "DISPLAY": self.display_var, "PULSE_SINK": self.pulse_sink_name}
        self.driver = webdriver.Chrome(options=options, service=Service(executable_path="/usr/local/bin/chromedriver", env=chrome_env))
        logger.info(f"web driver server for {self.output_destination} initialized at port {self.driver.service.port} on display {self.display_var}")

        with open("bots/webpage_streamer/webpage_streamer_payload.js", "r") as file:
            payload_code = file.read()

        combined_code = payload_code.replace("__ATTENDEE_OUTPUT_DESTINATION__", json.dumps(self.output_destination))

        # Add the combined script to execute on new document
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": combined_code})

    def start_gstreamer_capture(self):
        if self._gst_pipeline:
            return

        width, height = self.video_frame_size

        pipeline_desc = f"""
            ximagesrc display-name={self.display_var} use-damage=0 show-pointer=false
                ! video/x-raw,framerate=15/1,width={width},height={height}
                ! videoconvert
                ! video/x-raw,format=I420,width={width},height={height}
                ! queue max-size-buffers=5 max-size-time=0 leaky=downstream
                ! appsink name=video_sink emit-signals=false max-buffers=1 drop=true

            pulsesrc device={self.pulse_sink_name}.monitor
                ! audio/x-raw,format=S16LE,channels=1,rate=16000
                ! audioconvert
                ! audioresample
                ! queue max-size-buffers=8000 leaky=downstream
                ! appsink name=audio_sink emit-signals=false max-buffers=8000 drop=true
        """

        logger.info(f"Starting GStreamer capture pipeline for {self.output_destination}")
        self._gst_pipeline = Gst.parse_launch(pipeline_desc)

        self._gst_video_sink = self._gst_pipeline.get_by_name("video_sink")
        self._gst_audio_sink = self._gst_pipeline.get_by_name("audio_sink")

        if not self._gst_video_sink or not self._gst_audio_sink:
            raise RuntimeError("Failed to get GStreamer appsinks for audio/video")

        ret = self._gst_pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._gst_pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Failed to start GStreamer pipeline")

        logger.info(f"GStreamer capture pipeline for {self.output_destination} is PLAYING")

        self.video_track = GstVideoStreamTrack(
            sink=self._gst_video_sink,
            width=width,
            height=height,
            framerate=15,
        )
        self.audio_track = GstAudioStreamTrack(
            sink=self._gst_audio_sink,
            sample_rate=16000,
            channels=1,
        )

    def stop_gstreamer_capture(self):
        if self._gst_pipeline:
            logger.info(f"Stopping GStreamer capture pipeline for {self.output_destination}")
            self._gst_pipeline.set_state(Gst.State.NULL)
            self._gst_pipeline = None
            self._gst_video_sink = None
            self._gst_audio_sink = None
            self.video_track = None
            self.audio_track = None

    def stop(self):
        self.stop_gstreamer_capture()
        if self.driver:
            self.driver.quit()
        if self.display:
            self.display.stop()


class WebpageStreamer:
    def __init__(
        self,
        video_frame_size,
    ):
        self.video_frame_size = video_frame_size
        self.last_keepalive_time = None
        self.web_app = None
        # One browser per output destination. A browser that is never given a url sits on a blank page and is never captured.
        self.browsers = {
            "webcam": StreamedBrowser("webcam", video_frame_size, remote_debugging_port=9222, use_existing_display=True),
            "screenshare": StreamedBrowser("screenshare", video_frame_size, remote_debugging_port=9223, use_existing_display=False),
        }

    def run(self):
        for browser in self.browsers.values():
            browser.start()

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
            for browser in self.browsers.values():
                try:
                    browser.stop()
                except Exception as e:
                    logger.error(f"Error stopping {browser.output_destination} browser: {e}")
            if self.web_app:
                await self.web_app.shutdown()
            logger.info("Process shutting down")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        finally:
            os._exit(0)

    def load_webapp(self):
        pcs = set()

        # The *original* upstream meeting AudioStreamTracks, keyed by output destination, sent by the bot over each destination's peer connection.
        # The MediaRelay is necessary because it creates a small buffer. Without it audio quality is degraded.
        upstream_audio_relay = MediaRelay()
        upstream_audio_tracks = {}

        def get_output_destination(value):
            output_destination = value or DEFAULT_OUTPUT_DESTINATION
            if output_destination not in OUTPUT_DESTINATIONS:
                raise web.HTTPBadRequest(text=f"Invalid output_destination: {output_destination}")
            return output_destination

        async def offer_meeting_audio(req):
            """
            POST /offer_meeting_audio
            Return an SDP answer that *sends* the upstream audio (if present)
            to this new peer connection (listen-only client).
            """
            params = await req.json()
            offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
            output_destination = get_output_destination(req.query.get("output_destination"))

            # Every destination's upstream carries the same meeting audio, so fall back to any of them
            upstream = upstream_audio_tracks.get(output_destination) or next(iter(upstream_audio_tracks.values()), None)
            if upstream is None:
                return web.Response(status=409, text="No upstream audio has been published yet.")

            pc = RTCPeerConnection()
            pcs.add(pc)

            # Re-broadcast using the relay so multiple listeners are OK
            rebroadcast_track = upstream_audio_relay.subscribe(upstream)
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
            output_destination = get_output_destination(params.get("output_destination"))
            browser = self.browsers[output_destination]

            # Lazy-start capture so we don't accumulate latency before WebRTC is up
            browser.start_gstreamer_capture()

            pc = RTCPeerConnection()
            pcs.add(pc)

            # --- server-to-client: send GStreamer-captured video/audio ---
            v_track = browser.video_track
            a_track = browser.audio_track

            if v_track is not None:
                pc.addTrack(v_track)

            if a_track is not None:
                pc.addTrack(a_track)

            @pc.on("track")
            def on_track(track):
                if track.kind == "audio":
                    # store the ORIGINAL upstream track for rebroadcast
                    upstream_audio_tracks[output_destination] = track
                    logger.info(f"Upstream audio track for {output_destination} set for rebroadcast.")

            @pc.on("connectionstatechange")
            async def _on_state():
                if pc.connectionState in ("failed", "closed", "disconnected"):
                    await pc.close()
                    pcs.discard(pc)

            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

        async def start_streaming(req):
            data = await req.json()
            webpage_url = data.get("url")
            if not webpage_url:
                return web.json_response({"error": "URL is required"}, status=400)
            output_destination = get_output_destination(data.get("output_destination"))
            browser = self.browsers[output_destination]

            logger.info(f"Starting streaming to {webpage_url} for {output_destination}")
            # Page loads block, so run them off the event loop to avoid stalling the other destination's stream
            await asyncio.get_running_loop().run_in_executor(None, browser.driver.get, webpage_url)

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

        port = 8000

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

        web.run_app(app, host="0.0.0.0", port=port)
