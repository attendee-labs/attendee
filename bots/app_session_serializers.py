import logging

from rest_framework import serializers

from .serializers import BotSerializer, CreateBotSerializer

logger = logging.getLogger(__name__)

import jsonschema
from drf_spectacular.utils import (
    extend_schema_field,
)

ZOOM_RTMS_SCHEMA = {
    "type": "object",
    "properties": {
        "meeting_uuid": {
            "type": "string",
            "minLength": 1,
            "description": "The UUID of the Zoom meeting",
        },
        "rtms_stream_id": {
            "type": "string",
            "minLength": 1,
            "description": "The RTMS stream ID for the Zoom meeting",
        },
        "server_urls": {
            "type": "string",
            "minLength": 1,
            "description": "The server URL for the RTMS connection",
        },
        "operator_id": {
            "type": "string",
            "description": "The ID of the user who started the RTMS stream",
        },
    },
    "required": ["meeting_uuid", "rtms_stream_id", "server_urls"],
    "additionalProperties": False,
}


@extend_schema_field(ZOOM_RTMS_SCHEMA)
class ZoomRTMSJSONField(serializers.JSONField):
    pass


class CreateAppSessionSerializer(CreateBotSerializer):
    # Remove inherited required fields that don't apply to app sessions
    meeting_url = None
    bot_name = None
    join_at = None

    zoom_rtms = ZoomRTMSJSONField(help_text="Zoom RTMS configuration containing meeting UUID, stream ID, and server URLs", required=True)

    class Meta(BotSerializer.Meta):
        fields = [field for field in BotSerializer.Meta.fields if field not in ["name", "meeting_url", "join_at"]] + ["zoom_rtms"]

    def validate_zoom_rtms(self, value):
        if value is None:
            raise serializers.ValidationError("zoom_rtms is required")

        try:
            jsonschema.validate(instance=value, schema=ZOOM_RTMS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        from bots.ssrf import assert_safe_rtms_websocket_url

        server_urls = value.get("server_urls")
        try:
            assert_safe_rtms_websocket_url(server_urls)
        except ValueError as e:
            raise serializers.ValidationError({"server_urls": str(e)})

        return value

    def validate_transcription_settings(self, value):
        if value is None:
            value = {"meeting_closed_captions": {}}
        return super().validate_transcription_settings(value)

    def validate_recording_settings(self, value):
        if value is None:
            value = {}
        value["resolution"] = "720p"
        # Currently, we burn too much CPU with 1080p, so we'll only support 720p. Hopefully the RTMS Python SDK will let us support 1080p.
        return super().validate_recording_settings(value)


class AppSessionSerializer(BotSerializer):
    # Remove inherited required fields that don't apply to app sessions
    meeting_url = None
    bot_name = None
    join_at = None

    class Meta(BotSerializer.Meta):
        fields = [field for field in BotSerializer.Meta.fields if field not in ["name", "meeting_url", "join_at"]] + ["zoom_rtms_stream_id"]
