import logging
import time
from base64 import b64encode

from bots.models import RealtimeTriggerTypes

logger = logging.getLogger(__name__)


import audioop
import logging

logger = logging.getLogger(__name__)

SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1  # mono


def _downsample(chunk: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return chunk  # nothing to do

    # state None ⇒ filter state kept inside audioop (per-call ok for small chunks)
    converted, _ = audioop.ratecv(
        chunk,  # fragment
        SAMPLE_WIDTH,  # width
        CHANNELS,  # nchannels
        src_rate,  # inrate
        dst_rate,  # outrate
        None,  # state
    )
    return converted


def mixed_audio_websocket_payload(chunk: bytes, input_sample_rate: int, output_sample_rate: int, bot_object_id: str) -> dict:
    """
    Down-sample (if needed) and package for websocket.
    """
    chunk_downsampled = _downsample(chunk, input_sample_rate, output_sample_rate)

    return {
        "trigger": RealtimeTriggerTypes.type_to_api_code(RealtimeTriggerTypes.MIXED_AUDIO_CHUNK),
        "bot_id": bot_object_id,
        "data": {
            "chunk": b64encode(chunk_downsampled).decode("ascii"),
            "timestamp_ms": int(time.time() * 1000),
            "sample_rate": output_sample_rate,
        },
    }


def per_participant_video_websocket_payload(frame: bytes, bot_object_id: str, participant_id: str) -> dict:
    return {
        "trigger": RealtimeTriggerTypes.type_to_api_code(RealtimeTriggerTypes.PER_PARTICIPANT_VIDEO_FRAME),
        "bot_id": bot_object_id,
        "data": {
            "frame": b64encode(frame).decode("ascii"),
            "participant_id": participant_id,
        },
    }
