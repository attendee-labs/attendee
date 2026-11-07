import os
from unittest.mock import patch

from django.test import TestCase

from bots.models import Bot
from bots.serializers import CreateBotSerializer, PatchBotVoiceAgentSettingsSerializer


class TestPatchBotVoiceAgentSettingsSerializer(TestCase):
    def test_accepts_both_url_and_screenshare_url(self):
        serializer = PatchBotVoiceAgentSettingsSerializer(data={"url": "https://agent.example.com", "screenshare_url": "https://slides.example.com"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {"url": "https://agent.example.com", "screenshare_url": "https://slides.example.com"})

    def test_rejects_non_https_screenshare_url(self):
        serializer = PatchBotVoiceAgentSettingsSerializer(data={"url": "https://agent.example.com", "screenshare_url": "http://slides.example.com"})
        self.assertFalse(serializer.is_valid())
        self.assertIn("screenshare_url", serializer.errors)


@patch.dict(os.environ, {"ENABLE_VOICE_AGENTS": "true"})
class TestCreateBotVoiceAgentSettings(TestCase):
    def test_accepts_both_url_and_screenshare_url(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Test Bot",
                "voice_agent_settings": {"url": "https://agent.example.com", "screenshare_url": "https://slides.example.com"},
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["voice_agent_settings"],
            {"url": "https://agent.example.com", "screenshare_url": "https://slides.example.com", "reserve_resources": True},
        )


class TestVoiceAgentUrlsByOutputDestination(TestCase):
    def test_returns_both_urls(self):
        bot = Bot(settings={"voice_agent_settings": {"url": "https://agent.example.com", "screenshare_url": "https://slides.example.com"}})
        self.assertEqual(bot.voice_agent_urls_by_output_destination(), {"webcam": "https://agent.example.com", "screenshare": "https://slides.example.com"})

    def test_normalizes_missing_and_empty_urls_to_none(self):
        bot = Bot(settings={"voice_agent_settings": {"url": "", "reserve_resources": True}})
        self.assertEqual(bot.voice_agent_urls_by_output_destination(), {"webcam": None, "screenshare": None})

    def test_handles_missing_voice_agent_settings(self):
        bot = Bot(settings={})
        self.assertEqual(bot.voice_agent_urls_by_output_destination(), {"webcam": None, "screenshare": None})
