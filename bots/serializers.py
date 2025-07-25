import base64
import json
import os
from dataclasses import asdict

import jsonschema
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema_field,
    extend_schema_serializer,
)
from rest_framework import serializers

from .automatic_leave_configuration import AutomaticLeaveConfiguration
from .models import (
    Bot,
    BotChatMessageToOptions,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    ChatMessageToOptions,
    MediaBlob,
    MeetingTypes,
    ParticipantEventTypes,
    Recording,
    RecordingFormats,
    RecordingResolutions,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingViews,
    TranscriptionProviders,
)


def get_openai_model_enum():
    """Get allowed OpenAI models including custom env var if set"""
    default_models = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"]
    custom_model = os.getenv("OPENAI_MODEL_NAME")
    if custom_model and custom_model not in default_models:
        return default_models + [custom_model]
    return default_models


from .utils import is_valid_png, meeting_type_from_url, transcription_provider_from_bot_creation_data

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
                        "description": "The language code for transcription. Defaults to 'multi' if not specified, which selects the language automatically and can change the detected language in the middle of the audio. See here for available languages: https://developers.deepgram.com/docs/models-languages-overview.",
                    },
                    "detect_language": {
                        "type": "boolean",
                        "description": "Whether to automatically detect the spoken language. Can only detect a single language for the entire audio. This is only supported for an older model and is not recommended. Please use language='multi' instead.",
                    },
                    "callback": {
                        "type": "string",
                        "description": "The URL to send the transcriptions to. If used, the transcriptions will be sent directly from Deepgram to your server so you will not be able to access them via the Attendee API. See here for details: https://developers.deepgram.com/docs/callback",
                    },
                    "keyterms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Improve recall of key terms or phrases in the transcript. This feature is only available for the nova-3 model in english, so you must set the language to 'en'. See here for details: https://developers.deepgram.com/docs/keyterm",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Improve recall of key terms or phrases in the transcript. This feature is only available for the nova-2 model. See here for details: https://developers.deepgram.com/docs/keywords",
                    },
                    "model": {
                        "type": "string",
                        "description": "The model to use for transcription. Defaults to 'nova-3' if not specified, which is the recommended model for most use cases. See here for details: https://developers.deepgram.com/docs/models-languages-overview",
                    },
                    "redact": {"type": "array", "items": {"type": "string", "enum": ["pci", "pii", "numbers"]}, "uniqueItems": True, "description": "Array of redaction types to apply to transcription. Automatically removes or masks sensitive information like PII, PCI data, and numbers from transcripts. See here for details: https://developers.deepgram.com/docs/redaction"},
                },
                "additionalProperties": False,
            },
            "gladia": {
                "type": "object",
                "properties": {
                    "code_switching_languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The languages to transcribe the meeting in when using code switching. See here for available languages: https://docs.gladia.io/chapters/limits-and-specifications/languages",
                    },
                    "enable_code_switching": {"type": "boolean", "description": "Whether to use code switching to transcribe the meeting in multiple languages."},
                },
                "additionalProperties": False,
            },
            "meeting_closed_captions": {
                "type": "object",
                "properties": {
                    "google_meet_language": {
                        "type": "string",
                        "description": "The language code for Google Meet closed captions (e.g. 'en-US'). See here for available languages and codes: https://docs.google.com/spreadsheets/d/1MN44lRrEBaosmVI9rtTzKMii86zGgDwEwg4LSj-SjiE",
                    },
                    "teams_language": {
                        "type": "string",
                        "description": "The language code for Teams closed captions (e.g. 'en-us'). This will change the closed captions language for everyone in the meeting, not just the bot. See here for available languages and codes: https://docs.google.com/spreadsheets/d/1F-1iLJ_4btUZJkZcD2m5sF3loqGbB0vTzgOubwQTb5o/edit?usp=sharing",
                    },
                    "zoom_language": {"type": "string", "enum": ["Arabic", "Cantonese", "Chinese (Simplified)", "Czech", "Danish", "Dutch", "English", "Estonian", "Finnish", "French", "French (Canada)", "German", "Hebrew", "Hindi", "Hungarian", "Indonesian", "Italian", "Japanese", "Korean", "Malay", "Persian", "Polish", "Portuguese", "Romanian", "Russian", "Spanish", "Swedish", "Tagalog", "Tamil", "Telugu", "Thai", "Turkish", "Ukrainian", "Vietnamese"], "description": "The language to use for Zoom closed captions. (e.g. 'Spanish'). This will change the closed captions language for everyone in the meeting, not just the bot."},
                    "merge_consecutive_captions": {"type": "boolean", "description": "The captions from Google Meet can end in the middle of a sentence, which is not ideal. This setting deals with that by merging consecutive captions for a given speaker that occur close together in time. Turned off by default."},
                },
                "additionalProperties": False,
            },
            "openai": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": get_openai_model_enum(),
                        "description": "The OpenAI model to use for transcription",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional prompt to use for the OpenAI transcription",
                    },
                    "language": {
                        "type": "string",
                        "description": "The language to use for transcription. See here in the 'Set 1' column for available language codes: https://en.wikipedia.org/wiki/List_of_ISO_639_language_codes. This parameter is optional but if you know the language in advance, setting it will improve accuracy.",
                    },
                },
                "required": ["model"],
                "additionalProperties": False,
            },
            "assembly_ai": {
                "type": "object",
                "properties": {
                    "language_code": {"type": "string", "description": "The language code to use for transcription. See here for available languages: https://www.assemblyai.com/docs/speech-to-text/pre-recorded-audio/supported-languages"},
                    "language_detection": {"type": "boolean", "description": "Whether to automatically detect the spoken language."},
                    "keyterms_prompt": {"type": "array", "items": {"type": "string"}, "description": "List of words or phrases to boost in the transcript. Only supported for when using the 'slam-1' speech model. See AssemblyAI docs for details."},
                    "speech_model": {"type": "string", "enum": ["best", "nano", "slam-1", "universal"], "description": "The speech model to use for transcription. See AssemblyAI docs for details."},
                },
                "additionalProperties": False,
            },
            "sarvam": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": ["saarika:v2", "saarika:v2.5"],
                        "description": "The Sarvam model to use for transcription",
                    },
                    "language_code": {
                        "type": "string",
                        "enum": ["unknown", "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN", "en-IN", "gu-IN"],
                        "description": "The language code to use for transcription",
                    },
                },
                "required": [],
                "additionalProperties": False,
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
                "description": "The format of the recording to save. The supported formats are 'mp4' and 'mp3'.",
            },
            "view": {
                "type": "string",
                "description": "The view to use for the recording. The supported views are 'speaker_view' and 'gallery_view'.",
            },
            "resolution": {
                "type": "string",
                "description": "The resolution to use for the recording. The supported resolutions are '1080p' and '720p'. Defaults to '1080p'.",
                "enum": RecordingResolutions.values,
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


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "use_login": {
                "type": "boolean",
                "description": "Whether to use Teams bot login credentials to sign in before joining the meeting. Requires Teams bot login credentials to be set for the project.",
                "default": False,
            },
        },
        "required": [],
        "additionalProperties": False,
    }
)
class TeamsSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "sdk": {
                "type": "string",
                "enum": ["web", "native"],
                "description": "The Zoom SDK to use for the bot. Use 'web' when you need closed caption based transcription.",
                "default": "native",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
)
class ZoomSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "silence_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds of continuous silence after which the bot should leave",
                "default": 600,
            },
            "silence_activate_after_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait before activating the silence timeout",
                "default": 1200,
            },
            "only_participant_in_meeting_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait before leaving if bot is the only participant",
                "default": 60,
            },
            "wait_for_host_to_start_meeting_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait for the host to start the meeting",
                "default": 600,
            },
            "waiting_room_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait before leaving if the bot is in the waiting room",
                "default": 900,
            },
            "max_uptime_seconds": {
                "type": "integer",
                "description": "Maximum number of seconds that the bot should be running before automatically leaving (infinity)",
                "default": None,
            },
        },
        "required": [],
        "additionalProperties": False,
    }
)
class AutomaticLeaveSettingsJSONField(serializers.JSONField):
    pass


def get_webhook_trigger_enum():
    """Get available webhook trigger types from models"""
    from .models import WebhookTriggerTypes

    return list(WebhookTriggerTypes._get_mapping().values())


@extend_schema_field(
    {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The webhook URL (must be HTTPS)",
                },
                "triggers": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": get_webhook_trigger_enum(),
                    },
                    "description": "List of webhook trigger types",
                    "uniqueItems": True,
                },
            },
            "required": ["url", "triggers"],
            "additionalProperties": False,
        },
        "description": "List of webhook subscriptions for this bot",
    }
)
class WebhooksJSONField(serializers.JSONField):
    """Field for webhook subscriptions with validation"""

    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Chat message",
            value={
                "to": "everyone",
                "message": "Hello everyone, I'm here to record and summarize this meeting.",
            },
            description="An example of a chat message to send to everyone in the meeting",
        ),
        OpenApiExample(
            "Chat message to specific user",
            value={
                "to": "specific_user",
                "to_user_uuid": "123e4567-e89b-12d3-a456-426614174000",
                "message": "Hello Bob, I'm here to record and summarize this meeting.",
            },
            description="An example of a chat message to send to a specific user in the meeting",
        ),
    ]
)
class BotChatMessageRequestSerializer(serializers.Serializer):
    to_user_uuid = serializers.CharField(
        max_length=255,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="The UUID of the user to send the message to. Required if 'to' is 'specific_user'.",
    )
    to = serializers.ChoiceField(choices=BotChatMessageToOptions.values, help_text="Who to send the message to.", default=BotChatMessageToOptions.EVERYONE)
    message = serializers.CharField(help_text="The message text to send. Does not support emojis currently.")

    def validate(self, data):
        to_value = data.get("to")
        to_user_uuid = data.get("to_user_uuid")

        if to_value == BotChatMessageToOptions.SPECIFIC_USER and not to_user_uuid:
            raise serializers.ValidationError({"to_user_uuid": "This field is required when sending to a specific user."})

        return data

    def validate_message(self, value):
        if len(value) > 10000:
            raise serializers.ValidationError("Message must be less than 10000 characters")

        """Validate that the message only contains characters in the Basic Multilingual Plane (BMP)."""
        for char in value:
            if ord(char) > 0xFFFF:
                raise serializers.ValidationError("Message cannot contain emojis or rare script characters.")
        return value


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "audio": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the websocket to use for receiving meeting audio in real time and having the bot output audio in real time. It must start with wss://. See https://docs.attendee.dev/guides/realtime-audio-input-and-output for details on how to receive and send audio through the websocket connection.",
                    },
                    "sample_rate": {
                        "type": "integer",
                        "enum": [8000, 16000, 24000],
                        "default": 16000,
                        "description": "The sample rate of the audio to send. Can be 8000, 16000, or 24000. Defaults to 16000.",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            }
        },
        "required": [],
        "additionalProperties": False,
    }
)
class WebsocketSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "zoom_tokens_url": {
                "type": "string",
                "description": 'URL of an endpoint on your server that returns Zoom authentication tokens the bot will use when it joins the meeting. Our server will make a POST request to this URL with information about the bot and expects a JSON response with the format: {"zak_token": "<zak_token>", "join_token": "<join_token>", "app_privilege_token": "<app_privilege_token>"}. Not every token needs to be provided, i.e. you can reply with {"zak_token": "<zak_token>"}.',
            },
        },
        "required": [],
        "additionalProperties": False,
    }
)
class CallbackSettingsJSONField(serializers.JSONField):
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
    bot_image = BotImageSerializer(help_text="The image for the bot", required=False, default=None)
    metadata = MetadataJSONField(help_text="JSON object containing metadata to associate with the bot", required=False, default=None)
    bot_chat_message = BotChatMessageRequestSerializer(help_text="The chat message the bot sends after it joins the meeting", required=False, default=None)
    join_at = serializers.DateTimeField(help_text="The time the bot should join the meeting. ISO 8601 format, e.g. 2025-06-13T12:00:00Z", required=False, default=None)
    deduplication_key = serializers.CharField(help_text="Optional key for deduplicating bots. If a bot with this key already exists in a non-terminal state, the new bot will not be created and an error will be returned.", required=False, default=None)
    webhooks = WebhooksJSONField(
        help_text="List of webhook subscriptions to create for this bot. Each item should have 'url' and 'triggers' fields.",
        required=False,
        default=None,
    )

    callback_settings = CallbackSettingsJSONField(
        help_text="Callback urls for the bot to call when it needs to fetch certain data.",
        required=False,
        default=None,
    )

    WEBHOOKS_SCHEMA = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "pattern": "^https://.*",
                },
                "triggers": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": get_webhook_trigger_enum(),
                    },
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            "required": ["url", "triggers"],
            "additionalProperties": False,
        },
    }

    def validate_webhooks(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.WEBHOOKS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return value

    CALLBACK_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "zoom_tokens_url": {
                "type": "string",
                "pattern": "^https://.*",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_callback_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.CALLBACK_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # Validate that zoom_tokens_url is a proper HTTPS URL
        zoom_tokens_url = value.get("zoom_tokens_url")
        if zoom_tokens_url and not zoom_tokens_url.lower().startswith("https://"):
            raise serializers.ValidationError({"zoom_tokens_url": "URL must start with https://"})

        return value

    transcription_settings = TranscriptionSettingsJSONField(
        help_text="The transcription settings for the bot, e.g. {'deepgram': {'language': 'en'}}",
        required=False,
        default=None,
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
                    "callback": {"type": "string"},
                    "keyterms": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                    "redact": {"type": "array", "items": {"type": "string", "enum": ["pci", "pii", "numbers"]}, "uniqueItems": True, "description": "Array of redaction types to apply to transcription. Automatically removes or masks sensitive information like PII, PCI data, and numbers from transcripts."},
                },
                "additionalProperties": False,
            },
            "gladia": {
                "type": "object",
                "properties": {
                    "code_switching_languages": {"type": "array", "items": {"type": "string"}},
                    "enable_code_switching": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
            "openai": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": get_openai_model_enum(),
                        "description": "The OpenAI model to use for transcription",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional prompt to use for the OpenAI transcription",
                    },
                    "language": {
                        "type": "string",
                        "description": "The language to use for transcription. See here in the 'Set 1' column for available language codes: https://en.wikipedia.org/wiki/List_of_ISO_639_language_codes. This parameter is optional but if you know the language in advance, setting it will improve accuracy.",
                    },
                },
                "required": ["model"],
                "additionalProperties": False,
            },
            "assembly_ai": {
                "type": "object",
                "properties": {
                    "language_code": {"type": "string"},
                    "language_detection": {"type": "boolean"},
                    "keyterms_prompt": {"type": "array", "items": {"type": "string"}, "description": "List of words or phrases to boost in the transcript. See AssemblyAI docs for details."},
                    "speech_model": {"type": "string", "enum": ["best", "nano", "slam-1", "universal"], "description": "The speech model to use for transcription. See AssemblyAI docs for details."},
                },
                "required": [],
                "additionalProperties": False,
            },
            "meeting_closed_captions": {
                "type": "object",
                "properties": {
                    "google_meet_language": {"type": "string", "enum": ["af-ZA", "sq-AL", "am-ET", "ar-EG", "ar-x-LEVANT", "ar-x-MAGHREBI", "ar-x-GULF", "ar-AE", "hy-AM", "az-AZ", "eu-ES", "bn-BD", "bg-BG", "my-MM", "ca-ES", "cmn-Hans-CN", "cmn-Hant-TW", "cs-CZ", "nl-NL", "en-US", "en-AU", "en-IN", "en-PH", "en-GB", "et-EE", "fil-PH", "fi-FI", "fr-FR", "fr-CA", "gl-ES", "ka-GE", "de-DE", "el-GR", "gu-IN", "iw-IL", "hi-IN", "hu-HU", "is-IS", "id-ID", "it-IT", "ja-JP", "jv-ID", "kn-IN", "kk-KZ", "km-KH", "rw-RW", "ko-KR", "lo-LA", "lv-LV", "lt-LT", "mk-MK", "ms-MY", "ml-IN", "mr-IN", "mn-MN", "ne-NP", "nso-ZA", "nb-NO", "fa-IR", "pl-PL", "pt-BR", "pt-PT", "ro-RO", "ru-RU", "sr-RS", "st-ZA", "si-LK", "sk-SK", "sl-SI", "es-MX", "es-ES", "su-ID", "sw", "ss-latn-ZA", "sv-SE", "ta-IN", "te-IN", "th-TH", "ve-ZA", "tn-latn-ZA", "tr-TR", "uk-UA", "ur-PK", "uz-UZ", "vi-VN", "xh-ZA", "ts-ZA", "zu-ZA"]},
                    "teams_language": {
                        "type": "string",
                        "enum": ["ar-sa", "ar-ae", "bg-bg", "ca-es", "zh-cn", "zh-hk", "zh-tw", "hr-hr", "cs-cz", "da-dk", "nl-be", "nl-nl", "en-au", "en-ca", "en-in", "en-nz", "en-gb", "en-us", "et-ee", "fi-fi", "fr-ca", "fr-fr", "de-de", "de-ch", "el-gr", "he-il", "hi-in", "hu-hu", "id-id", "it-it", "ja-jp", "ko-kr", "lv-lv", "lt-lt", "nb-no", "pl-pl", "pt-br", "pt-pt", "ro-ro", "ru-ru", "sr-rs", "sk-sk", "sl-si", "es-mx", "es-es", "sv-se", "th-th", "tr-tr", "uk-ua", "vi-vn", "cy-gb"],
                    },
                    "zoom_language": {
                        "type": "string",
                        "enum": ["Arabic", "Cantonese", "Chinese (Simplified)", "Czech", "Danish", "Dutch", "English", "Estonian", "Finnish", "French", "French (Canada)", "German", "Hebrew", "Hindi", "Hungarian", "Indonesian", "Italian", "Japanese", "Korean", "Malay", "Persian", "Polish", "Portuguese", "Romanian", "Russian", "Spanish", "Swedish", "Tagalog", "Tamil", "Telugu", "Thai", "Turkish", "Ukrainian", "Vietnamese"],
                    },
                    "merge_consecutive_captions": {"type": "boolean", "description": "The captions from Google Meet can end in the middle of a sentence, which is not ideal. This setting deals with that by merging consecutive captions for a given speaker that occur close together in time. Turned off by default."},
                },
                "required": [],
                "additionalProperties": False,
            },
            "sarvam": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": ["saarika:v2", "saarika:v2.5"],
                        "description": "The Sarvam model to use for transcription",
                    },
                    "language_code": {
                        "type": "string",
                        "enum": ["unknown", "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN", "en-IN", "gu-IN"],
                        "description": "The language code to use for transcription",
                    },
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
            raise serializers.ValidationError("Invalid meeting URL")

        if meeting_type == MeetingTypes.GOOGLE_MEET:
            if not value.startswith("https://meet.google.com/"):
                raise serializers.ValidationError("Google Meet URL must start with https://meet.google.com/")

        return value

    def validate_transcription_settings(self, value):
        meeting_url = self.initial_data.get("meeting_url")
        meeting_type = meeting_type_from_url(meeting_url)
        use_zoom_web_adapter = self.initial_data.get("zoom_settings", {}).get("sdk", "native") == "web"

        # Set a default transcription_settings value if nothing given
        if value is None:
            if meeting_type == MeetingTypes.ZOOM:
                if use_zoom_web_adapter:
                    value = {"meeting_closed_captions": {}}
                else:
                    value = {"deepgram": {"language": "multi"}}
            elif meeting_type == MeetingTypes.GOOGLE_MEET:
                value = {"meeting_closed_captions": {}}
            elif meeting_type == MeetingTypes.TEAMS:
                value = {"meeting_closed_captions": {}}
            else:
                return None

        try:
            jsonschema.validate(instance=value, schema=self.TRANSCRIPTION_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # If deepgram key is specified but language is not, set to "multi"
        if "deepgram" in value and ("language" not in value["deepgram"] or value["deepgram"]["language"] is None):
            value["deepgram"]["language"] = "multi"

        initial_data_with_value = {**self.initial_data, "transcription_settings": value}

        if meeting_type == MeetingTypes.ZOOM and use_zoom_web_adapter:
            if transcription_provider_from_bot_creation_data(initial_data_with_value) != TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM:
                raise serializers.ValidationError({"transcription_settings": "API-based transcription is not supported for Zoom when using the web SDK. Please set 'zoom_settings.sdk' to 'native' in the bot creation request."})

        if meeting_type == MeetingTypes.ZOOM and not use_zoom_web_adapter:
            if transcription_provider_from_bot_creation_data(initial_data_with_value) == TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM:
                raise serializers.ValidationError({"transcription_settings": "Closed caption based transcription is not supported for Zoom when using the native SDK. Please set 'zoom_settings.sdk' to 'web' in the bot creation request."})

        if value.get("deepgram", {}).get("callback") and value.get("deepgram", {}).get("detect_language"):
            raise serializers.ValidationError({"transcription_settings": "Language detection is not supported for streaming transcription. Please pass language='multi' instead of detect_language=true."})

        return value

    websocket_settings = WebsocketSettingsJSONField(help_text="The websocket settings for the bot, e.g. {'audio': {'url': 'wss://example.com/audio', 'sample_rate': 16000}}", required=False, default=None)

    WEBSOCKET_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "audio": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the websocket to use for receiving meeting audio in real time and having the bot output audio in real time. It must start with wss://. See https://docs.attendee.dev/guides/realtime-audio-input-and-output for details on how to receive and send audio through the websocket connection.",
                    },
                    "sample_rate": {
                        "type": "integer",
                        "enum": [8000, 16000, 24000],
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_websocket_settings(self, value):
        if value is None:
            return value

        # Set default sample rate before validation
        if "audio" in value and value.get("audio"):
            if "sample_rate" not in value["audio"]:
                value["audio"]["sample_rate"] = 16000

        try:
            jsonschema.validate(instance=value, schema=self.WEBSOCKET_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # Validate websocket URL format if provided
        if "audio" in value and value.get("audio"):
            audio_url = value.get("audio", {}).get("url")
            if audio_url:
                if not audio_url.lower().startswith("wss://"):
                    raise serializers.ValidationError({"audio": {"url": "URL must start with wss://"}})

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
        default={"format": RecordingFormats.MP4, "view": RecordingViews.SPEAKER_VIEW, "resolution": RecordingResolutions.HD_1080P},
    )

    RECORDING_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "format": {"type": "string"},
            "view": {"type": "string"},
            "resolution": {
                "type": "string",
                "enum": list(RecordingResolutions.values),
            },
        },
        "required": [],
    }

    def validate_recording_settings(self, value):
        if value is None:
            return value

        # Define defaults
        defaults = {"format": RecordingFormats.MP4, "view": RecordingViews.SPEAKER_VIEW, "resolution": RecordingResolutions.HD_1080P}

        try:
            jsonschema.validate(instance=value, schema=self.RECORDING_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # If at least one attribute is provided, apply defaults for any missing attributes
        if value:
            for key, default_value in defaults.items():
                if key not in value:
                    value[key] = default_value

        # Validate format if provided
        format = value.get("format")
        if format not in [RecordingFormats.MP4, RecordingFormats.MP3, None]:
            raise serializers.ValidationError({"format": "Format must be mp4 or mp3"})

        # Validate view if provided
        view = value.get("view")
        if view not in [RecordingViews.SPEAKER_VIEW, RecordingViews.GALLERY_VIEW, None]:
            raise serializers.ValidationError({"view": "View must be speaker_view or gallery_view"})

        return value

    teams_settings = TeamsSettingsJSONField(
        help_text="The Microsoft Teams-specific settings for the bot.",
        required=False,
        default={"use_login": False},
    )

    TEAMS_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "use_login": {"type": "boolean"},
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_teams_settings(self, value):
        if value is None:
            return value

        # Define defaults
        defaults = {"use_login": False}

        try:
            jsonschema.validate(instance=value, schema=self.TEAMS_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # If at least one attribute is provided, apply defaults for any missing attributes
        if value:
            for key, default_value in defaults.items():
                if key not in value:
                    value[key] = default_value

        return value

    zoom_settings = ZoomSettingsJSONField(
        help_text="The Zoom-specific settings for the bot.",
        required=False,
        default={"sdk": "native"},
    )

    ZOOM_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "sdk": {"type": "string", "enum": ["web", "native"]},
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_zoom_settings(self, value):
        if value is None:
            return value

        # Define defaults
        defaults = {"sdk": "native"}

        try:
            jsonschema.validate(instance=value, schema=self.ZOOM_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # If at least one attribute is provided, apply defaults for any missing attributes
        if value:
            for key, default_value in defaults.items():
                if key not in value:
                    value[key] = default_value

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

    automatic_leave_settings = AutomaticLeaveSettingsJSONField(default=dict, required=False)

    def validate_automatic_leave_settings(self, value):
        # Set default values if not provided
        defaults = asdict(AutomaticLeaveConfiguration())

        # Validate that an unexpected key is not provided
        for key in value.keys():
            if key not in defaults.keys():
                raise serializers.ValidationError(f"Unexpected attribute: {key}")

        # Validate that all values are positive integers
        for param, default in defaults.items():
            if param in value and (not isinstance(value[param], int) or value[param] <= 0):
                raise serializers.ValidationError(f"{param} must be a positive integer")
            # Set default if not provided
            if param not in value:
                value[param] = default

        return value

    def validate_bot_name(self, value):
        """Validate that the bot name only contains characters in the Basic Multilingual Plane (BMP)."""
        for char in value:
            if ord(char) > 0xFFFF:
                raise serializers.ValidationError("Bot name cannot contain emojis or rare script characters.")
        return value

    def validate_join_at(self, value):
        """Validate that join_at cannot be in the past."""
        if value is None:
            return value

        if value < timezone.now():
            raise serializers.ValidationError("join_at cannot be in the past")

        return value

    def validate(self, data):
        """Validate that no unexpected fields are provided."""
        # Get all the field names defined in this serializer
        expected_fields = set(self.fields.keys())

        # Get all the fields provided in the input data
        provided_fields = set(self.initial_data.keys())

        # Check for unexpected fields
        unexpected_fields = provided_fields - expected_fields

        if unexpected_fields:
            raise serializers.ValidationError(f"Unexpected field(s): {', '.join(sorted(unexpected_fields))}. Allowed fields are: {', '.join(sorted(expected_fields))}")

        return data


class BotSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="object_id")
    metadata = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()
    transcription_state = serializers.SerializerMethodField()
    recording_state = serializers.SerializerMethodField()
    join_at = serializers.DateTimeField()

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
            "join_at",
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


class ChatMessageSerializer(serializers.Serializer):
    id = serializers.CharField(source="object_id")
    text = serializers.CharField()
    timestamp_ms = serializers.SerializerMethodField()
    timestamp = serializers.IntegerField()
    to = serializers.SerializerMethodField()
    sender_name = serializers.CharField(source="participant.full_name")
    sender_uuid = serializers.CharField(source="participant.uuid")
    sender_user_uuid = serializers.CharField(source="participant.user_uuid", allow_null=True)
    additional_data = serializers.JSONField()

    def get_to(self, obj):
        return ChatMessageToOptions.choices[obj.to - 1][1]

    def get_timestamp_ms(self, obj):
        return obj.timestamp * 1000


class ParticipantEventSerializer(serializers.Serializer):
    id = serializers.CharField(source="object_id")
    participant_name = serializers.CharField(source="participant.full_name")
    participant_uuid = serializers.CharField(source="participant.uuid")
    participant_user_uuid = serializers.CharField(source="participant.user_uuid", allow_null=True)
    event_type = serializers.SerializerMethodField()
    event_data = serializers.JSONField()
    timestamp_ms = serializers.IntegerField()

    def get_event_type(self, obj):
        return ParticipantEventTypes.type_to_api_code(obj.event_type)


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Update join_at",
            value={
                "join_at": "2025-06-13T12:00:00Z",
            },
            description="Example of updating the join_at time for a scheduled bot",
        )
    ]
)
class PatchBotSerializer(serializers.Serializer):
    join_at = serializers.DateTimeField(help_text="The time the bot should join the meeting. ISO 8601 format, e.g. 2025-06-13T12:00:00Z", required=False)

    def validate_join_at(self, value):
        """Validate that join_at cannot be in the past."""
        if value is None:
            return value

        if value < timezone.now():
            raise serializers.ValidationError("join_at cannot be in the past")

        return value
