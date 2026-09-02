import json

import requests
from google.cloud import texttospeech

from bots.models import Credentials
from bots.utils import mp3_to_pcm

ELEVENLABS_TEXT_TO_SPEECH_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"


def generate_audio_from_text(bot, text, settings, sample_rate):
    """
    Generate audio from text using text-to-speech settings.

    Args:
        bot (Bot): The bot instance
        text (str): The text to convert to speech
        settings (dict): Text-to-speech configuration settings containing:
            google:
                voice_language_code (str): Language code (e.g., "en-US")
                voice_name (str): Name of the voice to use
            elevenlabs:
                voice_language_code (str): Language code (e.g., "en")
                voice_id (str): ElevenLabs voice ID to use
        sample_rate (int): The sample rate in Hz
    Returns:
        tuple: (bytes, int) containing:
            - Audio data in LINEAR16 format
            - Duration in milliseconds
    """
    if "google" in settings:
        return generate_audio_from_text_via_google(bot=bot, text=text, settings=settings, sample_rate=sample_rate)
    if "elevenlabs" in settings:
        return generate_audio_from_text_via_elevenlabs(bot=bot, text=text, settings=settings, sample_rate=sample_rate)

    raise ValueError("Unsupported text-to-speech provider.")


def calculate_pcm_duration_ms(audio_content, sample_rate):
    bytes_per_sample = 2
    return int((len(audio_content) / bytes_per_sample / sample_rate) * 1000)


def generate_audio_from_text_via_google(bot, text, settings, sample_rate):
    google_tts_credentials = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.GOOGLE_TTS).first()

    if not google_tts_credentials:
        raise ValueError("Could not find Google Text-to-Speech credentials.")

    try:
        # Create client with credentials
        client = texttospeech.TextToSpeechClient.from_service_account_info(json.loads(google_tts_credentials.get_credentials().get("service_account_json", {})))
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError("Invalid Google Text-to-Speech credentials format: " + str(e)) from e
    except Exception as e:
        raise ValueError("Failed to initialize Google Text-to-Speech client: " + str(e)) from e

    # Set up text input
    synthesis_input = texttospeech.SynthesisInput(text=text)

    # Get Google settings
    google_settings = settings.get("google", {})
    language_code = google_settings.get("voice_language_code")
    voice_name = google_settings.get("voice_name")

    # Build voice parameters
    voice = texttospeech.VoiceSelectionParams(language_code=language_code, name=voice_name)

    # Configure audio output as PCM (LINEAR16)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=sample_rate,
    )

    # Perform the text-to-speech request
    response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)

    # Skip the WAV header (first 44 bytes) to get raw PCM data
    audio_content = response.audio_content[44:]

    return audio_content, calculate_pcm_duration_ms(audio_content, sample_rate)


def generate_audio_from_text_via_elevenlabs(bot, text, settings, sample_rate):
    elevenlabs_tts_credentials = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ELEVENLABS).first()
    if not elevenlabs_tts_credentials:
        raise ValueError("Could not find ElevenLabs credentials.")

    elevenlabs_credentials = elevenlabs_tts_credentials.get_credentials()
    api_key = elevenlabs_credentials.get("api_key") if elevenlabs_credentials else None
    if not api_key:
        raise ValueError("Could not find ElevenLabs credentials.")

    elevenlabs_settings = settings.get("elevenlabs", {})
    voice_language_code = elevenlabs_settings.get("voice_language_code")
    voice_id = elevenlabs_settings.get("voice_id")

    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "language_code": voice_language_code,
    }

    try:
        response = requests.post(
            f"{ELEVENLABS_TEXT_TO_SPEECH_URL}/{voice_id}",
            headers=headers,
            params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
            json=payload,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        raise ValueError("ElevenLabs text-to-speech request failed: " + str(e)) from e

    if response.status_code == 401:
        raise ValueError("ElevenLabs credentials are invalid.")
    if response.status_code != 200:
        raise ValueError(f"ElevenLabs text-to-speech failed with status code {response.status_code}: {response.text}")

    audio_content = mp3_to_pcm(response.content, sample_rate=sample_rate)
    return audio_content, calculate_pcm_duration_ms(audio_content, sample_rate)
