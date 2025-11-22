from enum import Enum


class BotPodSpecType(str, Enum):
    DEFAULT = "DEFAULT"
    SCHEDULED = "SCHEDULED"
