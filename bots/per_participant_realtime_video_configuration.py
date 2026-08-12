from dataclasses import dataclass

RESOLUTION_PARAMS = {
    "360p": {"width": 640, "height": 360, "framerate": 2.0, "jpeg_quality": 70},
    "720p": {"width": 1280, "height": 720, "framerate": 1.0, "jpeg_quality": 60},
    "1080p": {"width": 1920, "height": 1080, "framerate": 1.0, "jpeg_quality": 50},
}


@dataclass(frozen=True)
class PerParticipantRealtimeVideoSourceConfiguration:
    resolution: str = "360p"

    def __post_init__(self):
        if self.resolution not in ("none", "360p", "720p", "1080p"):
            raise ValueError(f"Invalid resolution: {self.resolution}. Must be one of: none, 360p, 720p, 1080p")

    @property
    def enabled(self) -> bool:
        return self.resolution != "none"

    @property
    def width(self) -> int:
        return RESOLUTION_PARAMS[self.resolution]["width"] if self.enabled else 0

    @property
    def height(self) -> int:
        return RESOLUTION_PARAMS[self.resolution]["height"] if self.enabled else 0

    @property
    def framerate(self) -> float:
        return RESOLUTION_PARAMS[self.resolution]["framerate"] if self.enabled else 0

    @property
    def jpeg_quality(self) -> int:
        return RESOLUTION_PARAMS[self.resolution]["jpeg_quality"] if self.enabled else 0

    def to_dict(self) -> dict:
        return {
            "resolution": self.resolution,
            "width": self.width,
            "height": self.height,
            "framerate": self.framerate,
            "jpeg_quality": self.jpeg_quality,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class PerParticipantRealtimeVideoConfiguration:
    webcam_configuration: PerParticipantRealtimeVideoSourceConfiguration = None
    screenshare_configuration: PerParticipantRealtimeVideoSourceConfiguration = None

    def __post_init__(self):
        if self.webcam_configuration is None:
            object.__setattr__(self, "webcam_configuration", PerParticipantRealtimeVideoSourceConfiguration())
        if self.screenshare_configuration is None:
            object.__setattr__(self, "screenshare_configuration", PerParticipantRealtimeVideoSourceConfiguration())

    def to_dict(self) -> dict:
        return {
            "webcam_configuration": self.webcam_configuration.to_dict(),
            "screenshare_configuration": self.screenshare_configuration.to_dict(),
        }
