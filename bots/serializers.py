import base64
import json
import logging
import os
from dataclasses import asdict

from django.conf import settings

logger = logging.getLogger(__name__)

import jsonschema
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema_field,
    extend_schema_serializer,
)
from rest_framework import serializers

from .automatic_leave_configuration import AutomaticLeaveConfiguration
from .models import (
    AsyncTranscription,
    AsyncTranscriptionStates,
    Bot,
    BotChatMessageToOptions,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Calendar,
    CalendarEvent,
    CalendarPlatform,
    CalendarStates,
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


def get_elevenlabs_language_codes():
    return [
        "afr",
        "amh",
        "ara",
        "asm",
        "ast",
        "aze",
        "bak",
        "bas",
        "bel",
        "ben",
        "bhr",
        "bod",
        "bos",
        "bre",
        "bul",
        "cat",
        "ceb",
        "ces",
        "chv",
        "ckb",
        "cnh",
        "cre",
        "cym",
        "dan",
        "dav",
        "deu",
        "div",
        "dyu",
        "ell",
        "eng",
        "epo",
        "est",
        "eus",
        "fao",
        "fas",
        "fil",
        "fin",
        "fra",
        "fry",
        "ful",
        "gla",
        "gle",
        "glg",
        "guj",
        "hat",
        "hau",
        "heb",
        "hin",
        "hrv",
        "hsb",
        "hun",
        "hye",
        "ibo",
        "ina",
        "ind",
        "isl",
        "ita",
        "jav",
        "jpn",
        "kab",
        "kan",
        "kas",
        "kat",
        "kaz",
        "kea",
        "khm",
        "kin",
        "kir",
        "kln",
        "kmr",
        "kor",
        "kur",
        "lao",
        "lat",
        "lav",
        "lij",
        "lin",
        "lit",
        "ltg",
        "ltz",
        "lug",
        "luo",
        "mal",
        "mar",
        "mdf",
        "mhr",
        "mkd",
        "mlg",
        "mlt",
        "mon",
        "mri",
        "mrj",
        "msa",
        "mya",
        "myv",
        "nan",
        "nep",
        "nhi",
        "nld",
        "nor",
        "nso",
        "nya",
        "oci",
        "ori",
        "orm",
        "oss",
        "pan",
        "pol",
        "por",
        "pus",
        "quy",
        "roh",
        "ron",
        "rus",
        "sah",
        "san",
        "sat",
        "sin",
        "skr",
        "slk",
        "slv",
        "smo",
        "sna",
        "snd",
        "som",
        "sot",
        "spa",
        "sqi",
        "srd",
        "srp",
        "sun",
        "swa",
        "swe",
        "tam",
        "tat",
        "tel",
        "tgk",
        "tha",
        "tig",
        "tir",
        "tok",
        "ton",
        "tsn",
        "tuk",
        "tur",
        "twi",
        "uig",
        "ukr",
        "umb",
        "urd",
        "uzb",
        "vie",
        "vot",
        "vro",
        "wol",
        "xho",
        "yid",
        "yor",
        "yue",
        "zgh",
        "zho",
        "zul",
        "zza",
    ]


from .meeting_url_utils import meeting_type_from_url, normalize_meeting_url
from .utils import is_valid_png, transcription_provider_from_bot_creation_data

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

# Define the schema once
TRANSCRIPTION_SETTINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "deepgram": {
            "type": "object",
            "properties": {
                "callback": {"description": "The URL to send the transcriptions to. If used, the transcriptions will be sent directly from Deepgram to your server so you will not be able to access them via the Attendee API. See here for details: https://developers.deepgram.com/docs/callback", "type": "string"},
                "detect_language": {"description": "Whether to automatically detect the spoken language. Can only detect a single language for the entire audio. This is only supported for an older model and is not recommended. Please use language='multi' instead.", "type": "boolean"},
                "keyterms": {"description": "Improve recall of key terms or phrases in the transcript. This feature is only available for the nova-3 model in english, so you must set the language to 'en'. See here for details: https://developers.deepgram.com/docs/keyterm", "items": {"type": "string"}, "type": "array"},
                "keywords": {"description": "Improve recall of key terms or phrases in the transcript. This feature is only available for the nova-2 model. See here for details: https://developers.deepgram.com/docs/keywords", "items": {"type": "string"}, "type": "array"},
                "language": {"description": "The language code for transcription. Defaults to 'multi' if not specified, which selects the language automatically and can change the detected language in the middle of the audio. See here for available languages: https://developers.deepgram.com/docs/models-languages-overview.", "type": "string"},
                "model": {"description": "The model to use for transcription. Defaults to 'nova-3' if not specified, which is the recommended model for most use cases. See here for details: https://developers.deepgram.com/docs/models-languages-overview", "type": "string"},
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
                "enable_code_switching": {
                    "type": "boolean",
                    "description": "Whether to use code switching to transcribe the meeting in multiple languages.",
                },
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
                "language_code": {
                    "type": "string",
                    "description": "The language code to use for transcription. See here for available languages: https://www.assemblyai.com/docs/speech-to-text/pre-recorded-audio/supported-languages",
                },
                "language_detection": {
                    "type": "boolean",
                    "description": "Whether to automatically detect the spoken language.",
                },
                "keyterms_prompt": {"type": "array", "items": {"type": "string"}, "description": "List of words or phrases to boost in the transcript. Only supported for when using the 'slam-1' speech model. See AssemblyAI docs for details."},
                "speech_model": {"type": "string", "enum": ["best", "nano", "slam-1", "universal"], "description": "The speech model to use for transcription. See AssemblyAI docs for details."},
                "speaker_labels": {"type": "boolean", "description": "Whether to enable AssemblyAI's ML-based diarization. Only needed if multiple people are speaking into a single microphone. Defaults to false."},
                "use_eu_server": {"type": "boolean", "description": "Whether to use the EU server for transcription. Defaults to false."},
                "language_detection_options": {"type": "object", "properties": {"expected_languages": {"type": "array", "items": {"type": "string"}}, "fallback_language": {"type": "string"}}, "description": "Options for controlling the automatic language detection. See AssemblyAI docs for details.", "additionalProperties": False},
            },
            "required": [],
            "additionalProperties": False,
        },
        "meeting_closed_captions": {
            "type": "object",
            "properties": {
                "google_meet_language": {
                    "type": "string",
                    "enum": ["af-ZA", "sq-AL", "am-ET", "ar-EG", "ar-x-LEVANT", "ar-x-MAGHREBI", "ar-x-GULF", "ar-AE", "hy-AM", "az-AZ", "eu-ES", "bn-BD", "bg-BG", "my-MM", "ca-ES", "cmn-Hans-CN", "cmn-Hant-TW", "cs-CZ", "nl-NL", "en-US", "en-AU", "en-IN", "en-PH", "en-GB", "et-EE", "fil-PH", "fi-FI", "fr-FR", "fr-CA", "gl-ES", "ka-GE", "de-DE", "el-GR", "gu-IN", "iw-IL", "hi-IN", "hu-HU", "is-IS", "id-ID", "it-IT", "ja-JP", "jv-ID", "kn-IN", "kk-KZ", "km-KH", "rw-RW", "ko-KR", "lo-LA", "lv-LV", "lt-LT", "mk-MK", "ms-MY", "ml-IN", "mr-IN", "mn-MN", "ne-NP", "nso-ZA", "nb-NO", "fa-IR", "pl-PL", "pt-BR", "pt-PT", "ro-RO", "ru-RU", "sr-RS", "st-ZA", "si-LK", "sk-SK", "sl-SI", "es-MX", "es-ES", "su-ID", "sw", "ss-latn-ZA", "sv-SE", "ta-IN", "te-IN", "th-TH", "ve-ZA", "tn-latn-ZA", "tr-TR", "uk-UA", "ur-PK", "uz-UZ", "vi-VN", "xh-ZA", "ts-ZA", "zu-ZA"],
                    "description": "The language code for Google Meet closed captions (e.g. 'en-US'). See here for available languages and codes: https://docs.google.com/spreadsheets/d/1MN44lRrEBaosmVI9rtTzKMii86zGgDwEwg4LSj-SjiE",
                },
                "teams_language": {
                    "type": "string",
                    "enum": ["ar-sa", "ar-ae", "bg-bg", "ca-es", "zh-cn", "zh-hk", "zh-tw", "hr-hr", "cs-cz", "da-dk", "nl-be", "nl-nl", "en-au", "en-ca", "en-in", "en-nz", "en-gb", "en-us", "et-ee", "fi-fi", "fr-ca", "fr-fr", "de-de", "de-ch", "el-gr", "he-il", "hi-in", "hu-hu", "id-id", "it-it", "ja-jp", "ko-kr", "lv-lv", "lt-lt", "nb-no", "pl-pl", "pt-br", "pt-pt", "ro-ro", "ru-ru", "sr-rs", "sk-sk", "sl-si", "es-mx", "es-es", "sv-se", "th-th", "tr-tr", "uk-ua", "vi-vn", "cy-gb"],
                    "description": "The language code for Teams closed captions (e.g. 'en-us'). This will change the closed captions language for everyone in the meeting, not just the bot. See here for available languages and codes: https://docs.google.com/spreadsheets/d/1F-1iLJ_4btUZJkZcD2m5sF3loqGbB0vTzgOubwQTb5o/edit?usp=sharing",
                },
                "zoom_language": {
                    "type": "string",
                    "enum": ["Arabic", "Cantonese", "Chinese (Simplified)", "Czech", "Danish", "Dutch", "English", "Estonian", "Finnish", "French", "French (Canada)", "German", "Hebrew", "Hindi", "Hungarian", "Indonesian", "Italian", "Japanese", "Korean", "Malay", "Persian", "Polish", "Portuguese", "Romanian", "Russian", "Spanish", "Swedish", "Tagalog", "Tamil", "Telugu", "Thai", "Turkish", "Ukrainian", "Vietnamese"],
                    "description": "The language to use for Zoom closed captions. (e.g. 'Spanish'). This will change the closed captions language for everyone in the meeting, not just the bot.",
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
        "elevenlabs": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "The ElevenLabs model to use for transcription", "enum": ["scribe_v1", "scribe_v1_experimental"]},
                "language_code": {
                    "type": "string",
                    "description": "An ISO-639-1 or ISO-639-3 language_code corresponding to the language of the audio file.",
                    "enum": get_elevenlabs_language_codes(),
                },
                "tag_audio_events": {"type": "boolean", "description": "Whether to tag audio events like 'laughter' in the transcription."},
            },
            "required": ["model_id"],
            "additionalProperties": False,
        },
    },
    "required": [],
    "additionalProperties": False,
}


class BotValidationMixin:
    """Mixin class providing meeting URL validation for serializers."""

    def validate_meeting_url(self, value):
        meeting_type, normalized_url = normalize_meeting_url(value)
        if meeting_type is None:
            logger.error(f"Invalid meeting URL: {value}")
            raise serializers.ValidationError("Invalid meeting URL")

        if normalized_url != value:
            logger.info(f"Normalized Meeting URL: {normalized_url} from {value}")
        return normalized_url

    def validate_join_at(self, value):
        """Validate that join_at cannot be in the past."""
        if value is None:
            return value

        if value < timezone.now():
            raise serializers.ValidationError("join_at cannot be in the past")

        if value > timezone.now() + relativedelta(years=3):
            raise serializers.ValidationError("join_at cannot be more than 3 years in the future")

        return value


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


@extend_schema_field(TRANSCRIPTION_SETTINGS_SCHEMA)
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
                "description": "The format of the recording to save. The supported formats are 'mp4', 'mp3' and 'none'.",
            },
            "view": {
                "type": "string",
                "description": "The view to use for the recording. The supported views are 'speaker_view', 'gallery_view' and 'speaker_view_no_sidebar'.",
            },
            "resolution": {
                "type": "string",
                "description": "The resolution to use for the recording. The supported resolutions are '1080p' and '720p'. Defaults to '1080p'.",
                "enum": RecordingResolutions.values,
            },
            "record_chat_messages_when_paused": {
                "type": "boolean",
                "description": "Whether to record chat messages even when the recording is paused. Defaults to false.",
                "default": False,
            },
            "record_async_transcription_audio_chunks": {
                "type": "boolean",
                "description": "Whether to record additional audio data which is needed for creating async (post-meeting) transcriptions. Defaults to false.",
                "default": False,
            },
        },
        "additionalProperties": False,
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
            "meeting_settings": {
                "type": "object",
                "properties": {
                    "allow_participants_to_unmute_self": {"type": "boolean"},
                    "allow_participants_to_share_whiteboard": {"type": "boolean"},
                    "allow_participants_to_request_cloud_recording": {"type": "boolean"},
                    "allow_participants_to_request_local_recording": {"type": "boolean"},
                    "allow_participants_to_share_screen": {"type": "boolean"},
                    "allow_participants_to_chat": {"type": "boolean"},
                    "enable_focus_mode": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
                "description": "Settings for various aspects of the Zoom Meeting. To use these settings, the bot must have host privileges.",
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
                "description": "Number of seconds to wait before leaving if bot becomes the only participant in the meeting because everyone else left.",
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


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL of a website containing a voice agent that gets the user's responses from the microphone. The bot will load this website and stream its video and audio to the meeting. The audio from the meeting will be sent to website via the microphone. See https://docs.attendee.dev/guides/voice-agents for further details.",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }
)
class VoiceAgentSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "bucket_name": {
                "type": "string",
                "description": "The name of the external storage bucket to use for media files.",
            },
            "recording_file_name": {
                "type": "string",
                "description": "Optional custom name for the recording file",
            },
        },
        "required": ["bucket_name"],
        "additionalProperties": False,
    }
)
class ExternalMediaStorageSettingsJSONField(serializers.JSONField):
    pass


class CreateAsyncTranscriptionSerializer(serializers.Serializer):
    transcription_settings = TranscriptionSettingsJSONField(help_text="The transcription settings to use for the async transcription.", required=True)

    def validate_transcription_settings(self, value):
        try:
            jsonschema.validate(instance=value, schema=TRANSCRIPTION_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        if "meeting_closed_captions" in value:
            raise serializers.ValidationError({"transcription_settings": "Meeting closed captions are not available for async transcription."})

        if value.get("deepgram", {}).get("callback"):
            raise serializers.ValidationError({"transcription_settings": "Deepgram callback is not available for async transcription."})

        if not value:
            raise serializers.ValidationError({"transcription_settings": "Please specify a transcription provider."})

        return value


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
class CreateBotSerializer(BotValidationMixin, serializers.Serializer):
    meeting_url = serializers.CharField(help_text="The URL of the meeting to join, e.g. https://zoom.us/j/123?pwd=456")
    bot_name = serializers.CharField(help_text="The name of the bot to create, e.g. 'My Bot'")
    bot_image = BotImageSerializer(help_text="The image for the bot", required=False, default=None)
    metadata = MetadataJSONField(help_text="JSON object containing metadata to associate with the bot", required=False, default=None)
    bot_chat_message = BotChatMessageRequestSerializer(help_text="The chat message the bot sends after it joins the meeting", required=False, default=None)
    join_at = serializers.DateTimeField(help_text="The time the bot should join the meeting. ISO 8601 format, e.g. 2025-06-13T12:00:00Z", required=False, default=None)
    calendar_event_id = serializers.CharField(help_text="The ID of the calendar event the bot should join.", required=False, default=None)
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

    external_media_storage_settings = ExternalMediaStorageSettingsJSONField(
        help_text="Settings that allow Attendee to upload the recording to an external storage bucket controlled by you. This relieves you from needing to download the recording from Attendee and then upload it to your own storage. To use this feature you must add credentials to your project that provide access to the external storage.",
        required=False,
        default=None,
    )

    voice_agent_settings = VoiceAgentSettingsJSONField(
        help_text="Settings for the voice agent that the bot should load.",
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

    EXTERNAL_MEDIA_STORAGE_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "bucket_name": {
                "type": "string",
            },
            "recording_file_name": {
                "type": "string",
            },
        },
        "required": ["bucket_name"],
        "additionalProperties": False,
    }

    def validate_external_media_storage_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.EXTERNAL_MEDIA_STORAGE_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return value

    VOICE_AGENT_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def validate_voice_agent_settings(self, value):
        if value is None:
            return value

        if os.getenv("ENABLE_VOICE_AGENTS", "false").lower() != "true":
            raise serializers.ValidationError("Voice agents are not enabled. Please set the ENABLE_VOICE_AGENTS environment variable to true to use voice agents.")

        try:
            jsonschema.validate(instance=value, schema=self.VOICE_AGENT_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # Validate that url is a proper URL
        url = value.get("url")
        if url and not url.lower().startswith("https://"):
            raise serializers.ValidationError({"url": "URL must start with https://"})

        if url:
            meeting_url = self.initial_data.get("meeting_url")
            meeting_type = meeting_type_from_url(meeting_url)
            use_zoom_web_adapter = self.initial_data.get("zoom_settings", {}).get("sdk", "native") == "web"

            if meeting_type == MeetingTypes.ZOOM and not use_zoom_web_adapter:
                raise serializers.ValidationError("Voice agent is not supported for Zoom when using the native SDK. Please set 'zoom_settings.sdk' to 'web' in the bot creation request.")

        return value

    transcription_settings = TranscriptionSettingsJSONField(
        help_text="The transcription settings for the bot, e.g. {'deepgram': {'language': 'en'}}",
        required=False,
        default=None,
    )

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
            jsonschema.validate(instance=value, schema=TRANSCRIPTION_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # If deepgram key is specified but language is not, set to "multi"
        if "deepgram" in value and ("language" not in value["deepgram"] or value["deepgram"]["language"] is None):
            value["deepgram"]["language"] = "multi"

        initial_data_with_value = {**self.initial_data, "transcription_settings": value}

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
        help_text="The settings for the bot's recording.",
        required=False,
        default={"format": RecordingFormats.MP4, "view": RecordingViews.SPEAKER_VIEW, "resolution": RecordingResolutions.HD_1080P, "record_chat_messages_when_paused": False, "record_async_transcription_audio_chunks": False},
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
            "record_chat_messages_when_paused": {"type": "boolean"},
            "record_async_transcription_audio_chunks": {"type": "boolean"},
        },
        "additionalProperties": False,
        "required": [],
    }

    def validate_recording_settings(self, value):
        if value is None:
            return value

        # Define defaults
        defaults = {"format": RecordingFormats.MP4, "view": RecordingViews.SPEAKER_VIEW, "resolution": RecordingResolutions.HD_1080P, "record_chat_messages_when_paused": False}

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
        if format not in [RecordingFormats.MP4, RecordingFormats.MP3, RecordingFormats.NONE, None]:
            raise serializers.ValidationError({"format": "Format must be mp4 or mp3 or 'none'"})

        # Validate view if provided
        view = value.get("view")
        if view not in [RecordingViews.SPEAKER_VIEW, RecordingViews.GALLERY_VIEW, RecordingViews.SPEAKER_VIEW_NO_SIDEBAR, None]:
            raise serializers.ValidationError({"view": "View must be speaker_view or gallery_view or speaker_view_no_sidebar"})

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
            "meeting_settings": {
                "type": "object",
                "properties": {
                    "allow_participants_to_unmute_self": {"type": "boolean"},
                    "allow_participants_to_share_whiteboard": {"type": "boolean"},
                    "allow_participants_to_request_cloud_recording": {"type": "boolean"},
                    "allow_participants_to_request_local_recording": {"type": "boolean"},
                    "allow_participants_to_share_screen": {"type": "boolean"},
                    "allow_participants_to_chat": {"type": "boolean"},
                    "enable_focus_mode": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
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

        # Make sure the total length of the stringified metadata is less than MAX_METADATA_LENGTH characters
        if len(json.dumps(value)) > settings.MAX_METADATA_LENGTH:
            raise serializers.ValidationError(f"Metadata must be less than {settings.MAX_METADATA_LENGTH} characters")

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
    deduplication_key = serializers.CharField()

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
            "deduplication_key",
        ]
        read_only_fields = fields


class TranscriptUtteranceSerializer(serializers.Serializer):
    speaker_name = serializers.CharField()
    speaker_uuid = serializers.CharField()
    speaker_user_uuid = serializers.CharField(allow_null=True)
    speaker_is_host = serializers.BooleanField()
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


class ParticipantSerializer(serializers.Serializer):
    id = serializers.CharField(source="object_id")
    name = serializers.CharField(source="full_name")
    uuid = serializers.CharField()
    user_uuid = serializers.CharField(allow_null=True)
    is_host = serializers.BooleanField()


class ParticipantEventSerializer(serializers.Serializer):
    id = serializers.CharField(source="object_id")
    participant_name = serializers.CharField(source="participant.full_name")
    participant_uuid = serializers.CharField(source="participant.uuid")
    participant_user_uuid = serializers.CharField(source="participant.user_uuid", allow_null=True)
    participant_is_host = serializers.BooleanField(source="participant.is_host")
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
class PatchBotSerializer(BotValidationMixin, serializers.Serializer):
    join_at = serializers.DateTimeField(help_text="The time the bot should join the meeting. ISO 8601 format, e.g. 2025-06-13T12:00:00Z", required=False)
    meeting_url = serializers.CharField(help_text="The URL of the meeting to join, e.g. https://zoom.us/j/123?pwd=456", required=False)


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Create Google Calendar",
            value={"client_id": "123456789-abcdefghijklmnopqrstuvwxyz.apps.googleusercontent.com", "client_secret": "GOCSPX-abcdefghijklmnopqrstuvwxyz", "refresh_token": "1//04abcdefghijklmnopqrstuvwxyz", "platform": "google", "metadata": {"tenant_id": "1234567890"}, "deduplication_key": "user-abcd"},
            description="Example of creating a Google calendar connection",
        ),
    ]
)
class CreateCalendarSerializer(serializers.Serializer):
    platform_uuid = serializers.CharField(help_text="The UUID of the calendar on the calendar platform. Specify only for non-primary calendars.", required=False, default=None)
    client_id = serializers.CharField(help_text="The client ID for the calendar platform authentication")
    client_secret = serializers.CharField(help_text="The client secret for the calendar platform authentication")
    refresh_token = serializers.CharField(help_text="The refresh token for accessing the calendar platform")
    platform = serializers.ChoiceField(choices=CalendarPlatform.choices, help_text="The calendar platform (google or microsoft)")
    metadata = serializers.JSONField(help_text="JSON object containing metadata to associate with the calendar", required=False, default=None)
    deduplication_key = serializers.CharField(help_text="Optional key for deduplicating calendars. If a calendar with this key already exists in the project, the new calendar will not be created and an error will be returned.", required=False, default=None)

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

        # Make sure the total length of the stringified metadata is less than MAX_METADATA_LENGTH characters
        if len(json.dumps(value)) > settings.MAX_METADATA_LENGTH:
            raise serializers.ValidationError(f"Metadata must be less than {settings.MAX_METADATA_LENGTH} characters")

        return value

    def validate_deduplication_key(self, value):
        if value is not None and len(value.strip()) == 0:
            raise serializers.ValidationError("Deduplication key cannot be empty")
        return value

    def validate_client_id(self, value):
        if not value or len(value.strip()) == 0:
            raise serializers.ValidationError("Client ID cannot be empty")
        return value.strip()

    def validate_client_secret(self, value):
        if not value or len(value.strip()) == 0:
            raise serializers.ValidationError("Client secret cannot be empty")
        return value.strip()

    def validate_refresh_token(self, value):
        if not value or len(value.strip()) == 0:
            raise serializers.ValidationError("Refresh token cannot be empty")
        return value.strip()

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


class CalendarSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="object_id")
    state = serializers.SerializerMethodField()
    metadata = serializers.SerializerMethodField()

    @extend_schema_field(
        {
            "type": "string",
            "enum": ["connected", "disconnected"],
        }
    )
    def get_state(self, obj):
        """Convert calendar state to API code"""
        mapping = {
            CalendarStates.CONNECTED: "connected",
            CalendarStates.DISCONNECTED: "disconnected",
        }
        return mapping.get(obj.state)

    @extend_schema_field({"type": "object", "description": "Metadata associated with the calendar"})
    def get_metadata(self, obj):
        return obj.metadata

    class Meta:
        model = Calendar
        fields = [
            "id",
            "platform",
            "client_id",
            "platform_uuid",
            "state",
            "metadata",
            "deduplication_key",
            "connection_failure_data",
            "created_at",
            "updated_at",
            "last_successful_sync_at",
            "last_attempted_sync_at",
        ]
        read_only_fields = fields


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Update credentials",
            value={"client_secret": "GOCSPX-NewClientSecret123", "refresh_token": "1//05o3zfluegTFVCgYICGHGAUSNgF-L9Ir23dcclPCJW7KmzPhsQaNFcAzNwQkV6uM1gIGID8nBelYDPtbIr123"},
            description="Example of updating calendar credentials",
        ),
        OpenApiExample(
            "Update metadata only",
            value={"metadata": {"department": "sales", "team": "frontend", "updated": "true"}},
            description="Example of updating only the calendar metadata",
        ),
        OpenApiExample(
            "Update refresh token only",
            value={"refresh_token": "1//05NewRefreshTokenHere"},
            description="Example of updating only the refresh token",
        ),
    ]
)
class PatchCalendarSerializer(serializers.Serializer):
    client_secret = serializers.CharField(help_text="The client secret for the calendar platform authentication", required=False)
    refresh_token = serializers.CharField(help_text="The refresh token for accessing the calendar platform", required=False)
    metadata = serializers.JSONField(help_text="JSON object containing metadata to associate with the calendar", required=False)

    def validate_client_secret(self, value):
        if value is not None:
            if not value or len(value.strip()) == 0:
                raise serializers.ValidationError("Client secret cannot be empty")
            return value.strip()
        return value

    def validate_refresh_token(self, value):
        if value is not None:
            if not value or len(value.strip()) == 0:
                raise serializers.ValidationError("Refresh token cannot be empty")
            return value.strip()
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

        # Make sure the total length of the stringified metadata is less than MAX_METADATA_LENGTH characters
        if len(json.dumps(value)) > settings.MAX_METADATA_LENGTH:
            raise serializers.ValidationError(f"Metadata must be less than {settings.MAX_METADATA_LENGTH} characters")

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


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Calendar Event",
            value={
                "id": "evt_abcdef1234567890",
                "calendar_id": "cal_abcdef1234567890",
                "platform_uuid": "google_event_123456789",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "name": "Event Name",
                "start_time": "2025-01-15T14:00:00Z",
                "end_time": "2025-01-15T15:00:00Z",
                "is_deleted": False,
                "attendees": [{"email": "user1@example.com", "name": "John Doe"}, {"email": "user2@example.com", "name": "Jane Smith"}],
                "raw": {"google_event_data": "..."},
                "bots": [{"id": "bot_abcdef1234567890", "metadata": {"customer_id": "abc123"}, "meeting_url": "https://meet.google.com/abc-defg-hij", "state": "joined_recording", "events": [], "transcription_state": "complete", "recording_state": "complete", "join_at": "2025-01-15T14:00:00Z"}],
                "created_at": "2025-01-13T10:30:00.123456Z",
                "updated_at": "2025-01-13T10:30:00.123456Z",
            },
            description="Example of a calendar event with associated bots",
        )
    ]
)
class CalendarEventSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="object_id")
    calendar_id = serializers.CharField(source="calendar.object_id")
    bots = serializers.SerializerMethodField()

    @extend_schema_field(BotSerializer(many=True))
    def get_bots(self, obj):
        """Get associated bots for this calendar event"""
        return BotSerializer(obj.bots.all(), many=True).data

    class Meta:
        model = CalendarEvent
        fields = [
            "id",
            "calendar_id",
            "platform_uuid",
            "meeting_url",
            "start_time",
            "end_time",
            "is_deleted",
            "attendees",
            "ical_uid",
            "name",
            "raw",
            "bots",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class AsyncTranscriptionSerializer(serializers.ModelSerializer):
    bot_id = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    id = serializers.CharField(source="object_id")

    class Meta:
        model = AsyncTranscription
        fields = ["bot_id", "id", "created_at", "updated_at", "state", "failure_data"]
        read_only_fields = fields

    def get_bot_id(self, obj):
        """Return the bot's object_id from the related recording"""
        return obj.recording.bot.object_id

    def get_state(self, obj):
        """Return the state as an API code"""
        return AsyncTranscriptionStates.state_to_api_code(obj.state)
