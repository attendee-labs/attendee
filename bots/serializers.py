import base64
import json

import jsonschema
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema_field,
    extend_schema_serializer,
)
from rest_framework import serializers

from .models import (
    Bot,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    MediaBlob,
    Recording,
    RecordingFormats,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingViews,
)
from .utils import is_valid_png, meeting_type_from_url

# Define the schema once
BOT_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["image/png"]},
        "data": {
            "type": "string",
        },
    },
    "required": ["type", "data"],
    "additionalProperties": False,
}


@extend_schema_field(BOT_IMAGE_SCHEMA)
class ImageJSONField(serializers.JSONField):
    """Field for images with validation"""

    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Valid image",
            value={
                "type": "image/png",
                "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
            },
            description="An image of a red pixel encoded in base64 in PNG format",
        )
    ]
)
class BotImageSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=[ct[0] for ct in MediaBlob.VALID_IMAGE_CONTENT_TYPES], help_text="Image content type. Currently only PNG is supported.")  # image/png
    data = serializers.CharField(help_text="Base64 encoded image data. Simple example of a red pixel encoded in PNG format: iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")  # base64 encoded image data

    def validate_type(self, value):
        """Validate the content type"""
        if value not in [ct[0] for ct in MediaBlob.VALID_IMAGE_CONTENT_TYPES]:
            raise serializers.ValidationError("Invalid image content type")
        return value

    def validate(self, data):
        """Validate the entire image data"""
        try:
            # Decode base64 data
            image_data = base64.b64decode(data.get("data", ""))
        except Exception:
            raise serializers.ValidationError("Invalid base64 encoded data")

        # Validate that it's a proper PNG image
        if not is_valid_png(image_data):
            raise serializers.ValidationError("Data is not a valid PNG image. This site can generate base64 encoded PNG images to test with: https://png-pixel.com")

        # Add the decoded data to the validated data
        data["decoded_data"] = image_data
        return data


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "deepgram": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "description": "The language code for transcription (e.g. 'en'). See here for available languages: https://developers.deepgram.com/docs/models-languages-overview",
                    },
                    "detect_language": {
                        "type": "boolean",
                        "description": "Whether to automatically detect the spoken language",
                    },
                },
            },
            "meeting_closed_captions": {
                "type": "object",
                "properties": {
                    "google_meet_language": {
                        "type": "string",
                        "description": "The language code for Google Meet closed captions (e.g. 'en-US'). See here for available languages and codes: https://docs.google.com/spreadsheets/d/1MN44lRrEBaosmVI9rtTzKMii86zGgDwEwg4LSj-SjiE",
                    },
                },
            },
        },
        "required": [],
    }
)
class TranscriptionSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "destination_url": {
                "type": "string",
                "description": "The URL of the RTMP server to send the stream to",
            },
            "stream_key": {
                "type": "string",
                "description": "The stream key to use for the RTMP server",
            },
        },
        "required": ["destination_url", "stream_key"],
    }
)
class RTMPSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "The format of the recording to save. The supported formats are 'mp4'.",
            },
            "view": {
                "type": "string",
                "description": "The view to use for the recording. The supported views are 'speaker_view' and 'gallery_view'.",
            },
        },
        "required": [],
    }
)
class RecordingSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "create_debug_recording": {
                "type": "boolean",
                "description": "Whether to generate a recording of the attempt to join the meeting. Used for debugging.",
            },
        },
        "required": [],
    }
)
class DebugSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field({"type": "object", "description": "JSON object containing metadata to associate with the bot", "example": {"client_id": "abc123", "user": "john_doe", "purpose": "Weekly team meeting"}})
class MetadataJSONField(serializers.JSONField):
    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Valid meeting URL",
            value={
                "meeting_url": "https://zoom.us/j/123?pwd=456",
                "bot_name": "My Bot",
            },
            description="Example of a valid Zoom meeting URL",
        )
    ]
)
class CreateBotSerializer(serializers.Serializer):
    meeting_url = serializers.CharField(help_text="The URL of the meeting to join, e.g. https://zoom.us/j/123?pwd=456")
    bot_name = serializers.CharField(help_text="The name of the bot to create, e.g. 'My Bot'")
    file_name = serializers.CharField(help_text="The name of the file to create, e.g. 'example_file_name' will create 'example_file_name.mp4'", default=None)
    bot_image = BotImageSerializer(help_text="The image for the bot", required=False, default=None)
    metadata = MetadataJSONField(help_text="JSON object containing metadata to associate with the bot", required=False, default=None)

    transcription_settings = TranscriptionSettingsJSONField(
        help_text="The transcription settings for the bot, e.g. {'deepgram': {'language': 'en'}}",
        required=False,
        default={"deepgram": {"language": "en"}},
    )

    TRANSCRIPTION_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "deepgram": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                    },
                    "detect_language": {"type": "boolean"},
                },
                "oneOf": [
                    {"required": ["language"]},
                    {"required": ["detect_language"]},
                ],
                "additionalProperties": False,
            },
            "meeting_closed_captions": {
                "type": "object",
                "properties": {
                    "google_meet_language": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_meeting_url(self, value):
        meeting_type = meeting_type_from_url(value)
        if meeting_type is None:
            raise serializers.ValidationError({"meeting_url": "Invalid meeting URL"})

        return value

    def validate_file_name(self, value):
        # None is legal but redundant because default value is also None
        if value is None:
            return value

        # File name must be a string
        if not isinstance(value, str):
            raise serializers.ValidationError({"file_name": "File name must be a string"})

        # File name must be less than 255 characters
        if len(value) > 255:
            raise serializers.ValidationError({"file_name": "File name must be less than 255 characters"})

        return value

    def validate_transcription_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.TRANSCRIPTION_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return value

    rtmp_settings = RTMPSettingsJSONField(
        help_text="RTMP server to stream to, e.g. {'destination_url': 'rtmp://global-live.mux.com:5222/app', 'stream_key': 'xxxx'}.",
        required=False,
        default=None,
    )

    RTMP_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "destination_url": {"type": "string"},
            "stream_key": {"type": "string"},
        },
        "required": ["destination_url", "stream_key"],
    }

    def validate_rtmp_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.RTMP_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # Validate RTMP URL format
        destination_url = value.get("destination_url", "")
        if not (destination_url.lower().startswith("rtmp://") or destination_url.lower().startswith("rtmps://")):
            raise serializers.ValidationError({"destination_url": "URL must start with rtmp:// or rtmps://"})

        return value

    recording_settings = RecordingSettingsJSONField(
        help_text="The settings for the bot's recording. Currently the only setting is 'view' which can be 'speaker_view' or 'gallery_view'.",
        required=False,
        default={"format": RecordingFormats.MP4, "view": RecordingViews.SPEAKER_VIEW},
    )

    RECORDING_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "format": {"type": "string"},
            "view": {"type": "string"},
        },
        "required": [],
    }

    def validate_recording_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.RECORDING_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # Validate format if provided
        format = value.get("format")
        if format not in [RecordingFormats.MP4, None]:
            raise serializers.ValidationError({"format": "Format must be mp4"})

        # Validate view if provided
        view = value.get("view")
        if view not in [RecordingViews.SPEAKER_VIEW, RecordingViews.GALLERY_VIEW, None]:
            raise serializers.ValidationError({"view": "View must be speaker_view or gallery_view"})

        return value

    debug_settings = DebugSettingsJSONField(
        help_text="The debug settings for the bot, e.g. {'create_debug_recording': True}.",
        required=False,
        default={"create_debug_recording": False},
    )

    DEBUG_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "create_debug_recording": {"type": "boolean"},
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_debug_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.DEBUG_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return value

    def validate_metadata(self, value):
        if value is None:
            return value

        # Check if it's a dict
        if not isinstance(value, dict):
            raise serializers.ValidationError("Metadata must be an object not an array or other type")

        # Make sure there is at least one key
        if not value:
            raise serializers.ValidationError("Metadata must have at least one key")

        # Check if all values are strings
        for key, val in value.items():
            if not isinstance(val, str):
                raise serializers.ValidationError(f"Value for key '{key}' must be a string")

        # Check if all keys are strings
        for key in value.keys():
            if not isinstance(key, str):
                raise serializers.ValidationError("All keys in metadata must be strings")

        # Make sure the total length of the stringified metadata is less than 1000 characters
        if len(json.dumps(value)) > 1000:
            raise serializers.ValidationError("Metadata must be less than 1000 characters")

        return value


class BotSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="object_id")
    metadata = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()
    transcription_state = serializers.SerializerMethodField()
    recording_state = serializers.SerializerMethodField()

    @extend_schema_field(
        {
            "type": "string",
            "enum": [BotStates.state_to_api_code(state.value) for state in BotStates],
        }
    )
    def get_state(self, obj):
        return BotStates.state_to_api_code(obj.state)

    @extend_schema_field({"type": "object", "description": "Metadata associated with the bot"})
    def get_metadata(self, obj):
        return obj.metadata

    @extend_schema_field(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "sub_type": {"type": "string", "nullable": True},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
        }
    )
    def get_events(self, obj):
        events = []
        for event in obj.bot_events.all():
            event_type = BotEventTypes.type_to_api_code(event.event_type)
            event_data = {"type": event_type, "created_at": event.created_at}

            if event.event_sub_type:
                event_data["sub_type"] = BotEventSubTypes.sub_type_to_api_code(event.event_sub_type)

            events.append(event_data)
        return events

    @extend_schema_field(
        {
            "type": "string",
            "enum": [RecordingTranscriptionStates.state_to_api_code(state.value) for state in RecordingTranscriptionStates],
        }
    )
    def get_transcription_state(self, obj):
        default_recording = Recording.objects.filter(bot=obj, is_default_recording=True).first()
        if not default_recording:
            return None

        return RecordingTranscriptionStates.state_to_api_code(default_recording.transcription_state)

    @extend_schema_field(
        {
            "type": "string",
            "enum": [RecordingStates.state_to_api_code(state.value) for state in RecordingStates],
        }
    )
    def get_recording_state(self, obj):
        default_recording = Recording.objects.filter(bot=obj, is_default_recording=True).first()
        if not default_recording:
            return None

        return RecordingStates.state_to_api_code(default_recording.state)

    class Meta:
        model = Bot
        fields = [
            "id",
            "metadata",
            "meeting_url",
            "state",
            "events",
            "transcription_state",
            "recording_state",
        ]
        read_only_fields = fields


class TranscriptUtteranceSerializer(serializers.Serializer):
    speaker_name = serializers.CharField()
    speaker_uuid = serializers.CharField()
    speaker_user_uuid = serializers.CharField(allow_null=True)
    timestamp_ms = serializers.IntegerField()
    duration_ms = serializers.IntegerField()
    transcription = serializers.JSONField()


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Recording Upload",
            value={
                "url": "https://attendee-short-term-storage-production.s3.amazonaws.com/e4da3b7fbbce2345d7772b0674a318d5.mp4?...",
                "start_timestamp_ms": 1733114771000,
            },
        )
    ]
)
class RecordingSerializer(serializers.ModelSerializer):
    start_timestamp_ms = serializers.IntegerField(source="first_buffer_timestamp_ms")

    class Meta:
        model = Recording
        fields = ["url", "start_timestamp_ms"]


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "google": {
                "type": "object",
                "properties": {
                    "voice_language_code": {
                        "type": "string",
                        "description": "The voice language code (e.g. 'en-US'). See https://cloud.google.com/text-to-speech/docs/voices for a list of available language codes and voices.",
                    },
                    "voice_name": {
                        "type": "string",
                        "description": "The name of the voice to use (e.g. 'en-US-Casual-K')",
                    },
                },
            }
        },
        "required": ["google"],
    }
)
class TextToSpeechSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Valid speech request",
            value={
                "text": "Hello, this is a bot speaking text.",
                "text_to_speech_settings": {
                    "google": {
                        "voice_language_code": "en-US",
                        "voice_name": "en-US-Casual-K",
                    }
                },
            },
            description="Example of a valid speech request",
        )
    ]
)
class SpeechSerializer(serializers.Serializer):
    text = serializers.CharField()
    text_to_speech_settings = TextToSpeechSettingsJSONField()

    TEXT_TO_SPEECH_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "google": {
                "type": "object",
                "properties": {
                    "voice_language_code": {"type": "string"},
                    "voice_name": {"type": "string"},
                },
                "required": ["voice_language_code", "voice_name"],
                "additionalProperties": False,
            }
        },
        "required": ["google"],
        "additionalProperties": False,
    }

    def validate_text_to_speech_settings(self, value):
        if value is None:
            return None

        try:
            jsonschema.validate(instance=value, schema=self.TEXT_TO_SPEECH_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return value
