"""
Utility functions and constants for transcription provider configuration.
"""
import os

from bots.models import TranscriptionProviders


# Map provider name strings to TranscriptionProviders enum values
PROVIDER_NAME_TO_ENUM_MAP = {
    "deepgram": TranscriptionProviders.DEEPGRAM,
    "gladia": TranscriptionProviders.GLADIA,
    "openai": TranscriptionProviders.OPENAI,
    "assembly_ai": TranscriptionProviders.ASSEMBLY_AI,
    "sarvam": TranscriptionProviders.SARVAM,
    "elevenlabs": TranscriptionProviders.ELEVENLABS,
    "kyutai": TranscriptionProviders.KYUTAI,
    "azure": TranscriptionProviders.AZURE,
    "custom_async": TranscriptionProviders.CUSTOM_ASYNC,
    "closed_caption_from_platform": TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM,
}

# Map provider name strings to transcription settings dict format
PROVIDER_NAME_TO_SETTINGS_MAP = {
    "deepgram": {"deepgram": {"language": "multi"}},
    "gladia": {"gladia": {}},
    "openai": {"openai": {}},
    "assembly_ai": {"assembly_ai": {}},
    "sarvam": {"sarvam": {}},
    "elevenlabs": {"elevenlabs": {}},
    "kyutai": {"kyutai": {}},
    "azure": {"azure": {}},
    "custom_async": {"custom_async": {}},
    "closed_caption_from_platform": {"meeting_closed_captions": {}},
}


def get_default_transcription_provider_from_env():
    """
    Get the default transcription provider from DEFAULT_TRANSCRIPTION_PROVIDER environment variable.
    
    Returns:
        TranscriptionProviders enum value or None if not set or invalid
    """
    default_provider_env = os.getenv("DEFAULT_TRANSCRIPTION_PROVIDER")
    if not default_provider_env:
        return None
    
    return PROVIDER_NAME_TO_ENUM_MAP.get(default_provider_env.lower())


def get_default_transcription_settings_from_env():
    """
    Get the default transcription settings dict from DEFAULT_TRANSCRIPTION_PROVIDER environment variable.
    
    Returns:
        dict with transcription settings or None if not set or invalid
    """
    default_provider_env = os.getenv("DEFAULT_TRANSCRIPTION_PROVIDER")
    if not default_provider_env:
        return None
    
    return PROVIDER_NAME_TO_SETTINGS_MAP.get(default_provider_env.lower())
