from django.test import TestCase

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.models import Bot, Project, Recording, RecordingTypes, TranscriptionProviders, TranscriptionTypes


class BotControllerTranscriptionDefaultsTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Org")
        project = Project.objects.create(name="Proj", organization=org)
        self.bot = Bot.objects.create(
            project=project,
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={
                "recording_settings": {"format": "mp4"},
                "transcription_runtime_settings": {
                    "silence_duration_seconds_override": 1.5,
                    "max_segment_seconds_override": 90,
                },
            },
        )
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=TranscriptionProviders.OPENAI,
            is_default_recording=True,
        )

    def test_custom_silence_and_segment_limits_are_used(self):
        controller = BotController(self.bot.id)
        self.assertEqual(controller.non_streaming_audio_silence_duration_limit(), 1.5)
        self.assertEqual(controller.non_streaming_audio_utterance_size_limit(), 48000 * 2 * 90)

    def test_automatic_provider_defaults_are_preserved(self):
        self.bot.settings = {
            "recording_settings": {"format": "mp4"},
            "transcription_runtime_settings": {},
        }
        self.bot.save()
        self.recording.transcription_provider = TranscriptionProviders.SARVAM
        self.recording.save()

        controller = BotController(self.bot.id)
        self.assertEqual(controller.non_streaming_audio_silence_duration_limit(), 1)
        self.assertEqual(controller.non_streaming_audio_utterance_size_limit(), 1920000)
