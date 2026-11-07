from django.test import TestCase

from bots.serializers import SpeechSerializer


class SpeechSerializerTest(TestCase):
    def test_accepts_elevenlabs_text_to_speech_settings(self):
        serializer = SpeechSerializer(
            data={
                "text": "Hello, this is a test speech",
                "text_to_speech_settings": {
                    "elevenlabs": {
                        "voice_language_code": "en",
                        "voice_id": "voice_123",
                    }
                },
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_multiple_text_to_speech_providers(self):
        serializer = SpeechSerializer(
            data={
                "text": "Hello, this is a test speech",
                "text_to_speech_settings": {
                    "google": {
                        "voice_language_code": "en-US",
                        "voice_name": "en-US-Casual-K",
                    },
                    "elevenlabs": {
                        "voice_language_code": "en",
                        "voice_id": "voice_123",
                    },
                },
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            serializer.errors["text_to_speech_settings"][0],
            "Exactly one text-to-speech provider must be specified. Supported providers are 'google' and 'elevenlabs'.",
        )

    def test_rejects_missing_elevenlabs_voice_id(self):
        serializer = SpeechSerializer(
            data={
                "text": "Hello, this is a test speech",
                "text_to_speech_settings": {
                    "elevenlabs": {
                        "voice_language_code": "en",
                    }
                },
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors["text_to_speech_settings"][0], "'voice_id' is a required property")
