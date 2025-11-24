#!/usr/bin/env bash
set -euo pipefail

# --- Xvfb setup (needed for x11grab) ---
export DISPLAY=${DISPLAY:-:99}
Xvfb "$DISPLAY" -screen 0 1930x1090x24 >/tmp/xvfb.log 2>&1 &
sleep 1  # allow Xvfb to start

# --- PulseAudio per-user mode ---
export XDG_RUNTIME_DIR=/tmp/pulse-runtime
mkdir -p "$XDG_RUNTIME_DIR"

pulseaudio --daemonize=yes --log-target=stderr --disallow-exit --exit-idle-time=-1 --realtime=no || true

# Wait for PulseAudio to be ready
for i in $(seq 1 15); do
  pactl info >/dev/null 2>&1 && break
  echo "Waiting for PulseAudio per-user daemon..."
  sleep 1
done

# --- Null sink for meeting audio ---
if ! pactl list short sinks | grep -q meet; then
  pactl load-module module-null-sink sink_name=meet sink_properties=device.description=Meet >/dev/null
fi
pactl set-default-sink meet
pactl set-default-source meet.monitor

# --- ALSA to PulseAudio routing ---
mkdir -p "$HOME"
cat > "$HOME/.asoundrc" <<'EOF'
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

# --- Optional ffmpeg diagnostics ---
export FFREPORT='file=/tmp/ffreport-%p-%t.log:level=32'

# --- Quick diagnostics (optional) ---
echo "Sinks:"; pactl list short sinks | sed 's/^/  /' || true
echo "Sources:"; pactl list short sources | sed 's/^/  /' || true
echo "ALSA sees pulse device?"; (arecord -L | grep -i pulse || echo "NO_PULSE_PLUGIN")

# --- Run Celery worker ---
exec celery -A attendee worker -l INFO -Q $CUSTOM_QUEUE_NAME