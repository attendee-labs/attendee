#!/bin/bash
# Smoke test for the Attendee images (Dockerfile and Dockerfile.debian), run
# inside them by .github/workflows/ci.yml. Checks the native libraries, GStreamer
# elements, FFmpeg, Chrome and command-line tools that the bots rely on, so a
# package missing from either image fails CI.
set -euo pipefail

cd /attendee

python3 - <<'PY'
import importlib
import sys

failed = []
for name in ["av", "cv2", "numpy", "zoom_meeting_sdk", "psycopg2", "webrtcvad", "aiortc",
             "livekit", "lxml", "signxml", "cryptography", "pydub", "grpc"]:
    try:
        importlib.import_module(name)
    except Exception as e:
        failed.append(f"import {name}: {e!r}")

# The webpage streamer builds frames with PyAV and hands them to aiortc.
try:
    from av import AudioFrame, VideoFrame

    VideoFrame(format="yuv420p", width=64, height=48)
    AudioFrame(format="s16", layout="mono", samples=160)
except Exception as e:
    failed.append(f"av frames: {e!r}")

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GstApp  # noqa: F401

Gst.init(None)
for element in ["appsrc", "appsink", "h264parse", "x264enc", "voaacenc", "videorate", "videoconvert",
                "mp4mux", "flacenc", "filesink", "audiorate", "audioconvert", "videotestsrc", "qtdemux",
                "avdec_h264", "opusenc", "webmmux", "vp8enc", "pulsesrc", "alsasrc", "ximagesrc"]:
    if Gst.ElementFactory.find(element) is None:
        failed.append(f"gstreamer element {element}")

if failed:
    print("FAILED:", *failed, sep="\n  ")
    sys.exit(1)
print("python imports and gstreamer elements ok")
PY

# No Python extension, GStreamer plugin, FFmpeg or Chrome may link to a library
# that isn't installed. Libraries bundled in wheels (*.libs/) resolve through the
# extension's RPATH, so they're only checked through it; Chrome's Qt shims are
# optional and unused headless.
site_packages=$(python3 -c 'import site; print(" ".join(site.getsitepackages()))')
unresolved=$(find $site_packages /usr/lib/x86_64-linux-gnu/gstreamer-1.0 /opt/google/chrome /usr/bin/ffmpeg \
        -type f \( -name '*.so' -o -name '*.so.*' -o -name chrome -o -name ffmpeg \) 2>/dev/null \
    | grep -v -e '\.libs/' -e '/libqt[56]_shim\.so$' \
    | while read -r f; do
        # Capture first: under pipefail, grep -q exiting early could fail ldd with SIGPIPE.
        deps=$(ldd "$f" 2>/dev/null) || true
        if [[ $deps == *"not found"* ]]; then echo "$f"; fi
    done || true)
if [ -n "$unresolved" ]; then
    echo "FAILED: unresolved shared libraries in:"
    printf '  %s\n' $unresolved
    for f in $unresolved; do ldd "$f" | grep 'not found'; done
    exit 1
fi
echo "shared libraries ok"

# Commands the bots and entrypoint.sh run.
for cmd in Xvfb xterm xclip pulseaudio pactl ffmpeg google-chrome chromedriver; do
    command -v "$cmd" >/dev/null || { echo "FAILED: command $cmd not found"; exit 1; }
done
chromedriver --version

ffmpeg -hide_banner -loglevel error -f lavfi -i sine=d=1 -f lavfi -i testsrc=d=1 \
    -c:v libx264 -c:a aac -y /tmp/smoke-test.mp4
echo "ffmpeg encode ok"

google-chrome --version
timeout 120 google-chrome --headless=new --no-sandbox --disable-gpu \
    --dump-dom 'data:text/html,<p>smoke-test</p>' 2>/dev/null | grep -q '<p>smoke-test</p>'
echo "chrome headless ok"
