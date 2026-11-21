import logging
import os
import subprocess
import uuid

logger = logging.getLogger(__name__)

PULSE_SINK_ENV_VAR = "PULSE_SINK"
PULSE_SOURCE_ENV_VAR = "PULSE_SOURCE"


def per_bot_audio_sink_enabled():
    return os.getenv("PER_BOT_AUDIO_SINK", "true").lower() == "true"


def audio_input_ffmpeg_args():
    source = os.environ.get(PULSE_SOURCE_ENV_VAR)
    if source:
        return ["-f", "pulse", "-i", source]
    return ["-f", "alsa", "-i", "default"]


def audio_sink_to_mute():
    return os.environ.get(PULSE_SINK_ENV_VAR, "@DEFAULT_SINK@")


class PerBotAudioSink:
    def __init__(self, bot_id, shares_audio_server_with_other_bots):
        self.sink_name = f"attendee_bot_{bot_id}_{uuid.uuid4().hex[:8]}"
        self.shares_audio_server_with_other_bots = shares_audio_server_with_other_bots
        self.module_id = None

    @property
    def monitor_name(self):
        return f"{self.sink_name}.monitor"

    def __enter__(self):
        if not self.shares_audio_server_with_other_bots:
            return self

        if not per_bot_audio_sink_enabled():
            logger.info("PER_BOT_AUDIO_SINK is false, recording from the shared default sink")
            return self

        subprocess.run(["pactl", "unload-module", "module-switch-on-connect"], capture_output=True, text=True)

        try:
            result = subprocess.run(
                ["pactl", "load-module", "module-null-sink", f"sink_name={self.sink_name}", f"sink_properties=device.description={self.sink_name}"],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception:
            logger.exception(f"Failed to create audio sink {self.sink_name}, recording from the shared default sink")
            return self

        self.module_id = result.stdout.strip()
        os.environ[PULSE_SINK_ENV_VAR] = self.sink_name
        os.environ[PULSE_SOURCE_ENV_VAR] = self.monitor_name
        logger.info(f"Created audio sink {self.sink_name} (module {self.module_id})")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.module_id is None:
            return False

        os.environ.pop(PULSE_SINK_ENV_VAR, None)
        os.environ.pop(PULSE_SOURCE_ENV_VAR, None)

        try:
            subprocess.run(["pactl", "unload-module", self.module_id], capture_output=True, text=True, check=True)
            logger.info(f"Unloaded audio sink {self.sink_name} (module {self.module_id})")
        except Exception:
            logger.exception(f"Failed to unload audio sink module {self.module_id}")

        self.module_id = None
        return False
