from dataclasses import dataclass
from itertools import combinations
from typing import FrozenSet


# Specifies how the bot will use the media from the meeting platform
# For now there are only a few valid configurations, to avoid having to make the bot
# work for every possible configuration
@dataclass(frozen=True)
class PipelineConfiguration:
    record_video: bool
    record_audio: bool
    transcribe_audio: bool
    rtmp_stream_audio: bool
    rtmp_stream_video: bool
    websocket_stream_audio: bool
    websocket_stream_per_participant_audio: bool
    websocket_stream_per_participant_video: bool

    def __post_init__(self):
        base_configurations_that_support_websocket_addons = [
            # Basic meeting bot configuration
            frozenset({"record_audio", "record_video", "transcribe_audio"}),
            # Audio only recording configuration
            frozenset({"record_audio", "transcribe_audio"}),
            # Pure transcription configuration
            frozenset({"transcribe_audio"}),
        ]

        base_configurations_that_do_not_support_websocket_addons = [
            # RTMP streaming configuration
            frozenset({"rtmp_stream_audio", "rtmp_stream_video", "transcribe_audio"}),
        ]

        websocket_add_ons = [
            "websocket_stream_audio",
            "websocket_stream_per_participant_audio",
            "websocket_stream_per_participant_video",
        ]

        valid_configurations: FrozenSet[FrozenSet[str]] = frozenset(
            [
                *base_configurations_that_do_not_support_websocket_addons,
                *(base | frozenset(combo) for base in base_configurations_that_support_websocket_addons for r in range(len(websocket_add_ons) + 1) for combo in combinations(websocket_add_ons, r)),
            ]
        )

        # Get the set of all fields that are True
        active_fields = frozenset(field for field in self.__dataclass_fields__.keys() if getattr(self, field))

        # Check if this combination exists in our valid configurations
        if active_fields not in valid_configurations:
            raise ValueError(f"Invalid configuration: {active_fields}\nMust be one of: {valid_configurations}")

    @classmethod
    def recorder_bot(cls, websocket_stream_audio=False, websocket_stream_per_participant_audio=False, websocket_stream_per_participant_video=False) -> "PipelineConfiguration":
        return cls(
            record_video=True,
            record_audio=True,
            transcribe_audio=True,
            rtmp_stream_audio=False,
            rtmp_stream_video=False,
            websocket_stream_audio=websocket_stream_audio,
            websocket_stream_per_participant_audio=websocket_stream_per_participant_audio,
            websocket_stream_per_participant_video=websocket_stream_per_participant_video,
        )

    @classmethod
    def audio_recorder_bot(cls, websocket_stream_audio=False, websocket_stream_per_participant_audio=False, websocket_stream_per_participant_video=False) -> "PipelineConfiguration":
        return cls(
            record_video=False,
            record_audio=True,
            transcribe_audio=True,
            rtmp_stream_audio=False,
            rtmp_stream_video=False,
            websocket_stream_audio=websocket_stream_audio,
            websocket_stream_per_participant_audio=websocket_stream_per_participant_audio,
            websocket_stream_per_participant_video=websocket_stream_per_participant_video,
        )

    @classmethod
    def pure_transcription_bot(cls, websocket_stream_audio=False, websocket_stream_per_participant_audio=False, websocket_stream_per_participant_video=False) -> "PipelineConfiguration":
        return cls(
            record_video=False,
            record_audio=False,
            transcribe_audio=True,
            rtmp_stream_audio=False,
            rtmp_stream_video=False,
            websocket_stream_audio=websocket_stream_audio,
            websocket_stream_per_participant_audio=websocket_stream_per_participant_audio,
            websocket_stream_per_participant_video=websocket_stream_per_participant_video,
        )

    @classmethod
    def rtmp_streaming_bot(cls) -> "PipelineConfiguration":
        return cls(
            record_video=False,
            record_audio=False,
            transcribe_audio=True,
            rtmp_stream_audio=True,
            rtmp_stream_video=True,
            websocket_stream_audio=False,
            websocket_stream_per_participant_audio=False,
            websocket_stream_per_participant_video=False,
        )
