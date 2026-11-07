from unittest.mock import MagicMock, patch

from django.test import TestCase

from accounts.models import Organization
from bots.bot_controller.text_to_speech import generate_audio_from_text
from bots.models import Bot, Credentials, Project


class GenerateAudioFromTextTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(project=self.project, name="Test Bot", meeting_url="https://zoom.us/j/1234567890")

    @patch("bots.bot_controller.text_to_speech.mp3_to_pcm")
    @patch("bots.bot_controller.text_to_speech.requests.post")
    def test_generates_audio_via_elevenlabs(self, mock_post, mock_mp3_to_pcm):
        elevenlabs_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ELEVENLABS)
        elevenlabs_credentials.set_credentials({"api_key": "test-api-key"})

        mock_post.return_value = MagicMock(status_code=200, content=b"fake-mp3")
        mock_mp3_to_pcm.return_value = b"\x00\x00" * 44100

        audio_content, duration_ms = generate_audio_from_text(
            bot=self.bot,
            text="Hello, this is a test speech",
            settings={
                "elevenlabs": {
                    "voice_language_code": "en",
                    "voice_id": "voice_123",
                }
            },
            sample_rate=44100,
        )

        self.assertEqual(audio_content, b"\x00\x00" * 44100)
        self.assertEqual(duration_ms, 1000)
        mock_post.assert_called_once_with(
            "https://api.elevenlabs.io/v1/text-to-speech/voice_123",
            headers={
                "xi-api-key": "test-api-key",
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            params={"output_format": "mp3_44100_128"},
            json={
                "text": "Hello, this is a test speech",
                "language_code": "en",
            },
            timeout=30,
        )
        mock_mp3_to_pcm.assert_called_once_with(b"fake-mp3", sample_rate=44100)

    def test_raises_when_elevenlabs_credentials_are_missing(self):
        with self.assertRaisesMessage(ValueError, "Could not find ElevenLabs credentials."):
            generate_audio_from_text(
                bot=self.bot,
                text="Hello, this is a test speech",
                settings={
                    "elevenlabs": {
                        "voice_language_code": "en",
                        "voice_id": "voice_123",
                    }
                },
                sample_rate=44100,
            )
