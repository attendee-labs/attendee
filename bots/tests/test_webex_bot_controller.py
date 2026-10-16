from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.bot_controller.gstreamer_pipeline import GstreamerPipeline
from bots.bot_controller.main_thread_executor import MainThreadExecutor
from bots.bots_api_utils import validate_meeting_url_and_credentials
from bots.models import Bot, Credentials, Project
from bots.serializers import CreateBotSerializer
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

WEBEX_URL = "https://acme.my.webex.com/meet/jane.doe"
WEBEX_CREDENTIALS = {"client_id": "client-id", "client_secret": "client-secret", "refresh_token": "refresh-token"}


class WebexBotTestCase(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def add_credentials(self):
        Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.WEBEX_SERVICE_APP).set_credentials(WEBEX_CREDENTIALS)


class WebexBotCreationTest(WebexBotTestCase):
    def test_creating_a_webex_bot_requires_the_service_app_credentials(self):
        error = validate_meeting_url_and_credentials(WEBEX_URL, self.project)

        self.assertEqual(error, {"error": f"Webex Service App credentials are required to create a Webex bot. Please add them at http://localhost:8000/projects/{self.project.object_id}/credentials"})

    def test_a_webex_bot_with_the_credentials_is_valid(self):
        self.add_credentials()

        self.assertIsNone(validate_meeting_url_and_credentials(WEBEX_URL, self.project))

    def test_other_platforms_do_not_need_the_webex_credentials(self):
        self.assertIsNone(validate_meeting_url_and_credentials("https://meet.google.com/abc-defg-hij", self.project))

    def test_a_webex_bot_is_transcribed_with_the_meeting_captions_by_default(self):
        serializer = CreateBotSerializer(data={"meeting_url": WEBEX_URL, "bot_name": "Test Bot"})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["transcription_settings"], {"meeting_closed_captions": {}})
        self.assertEqual(serializer.validated_data["meeting_url"], WEBEX_URL)

    def test_a_webex_bot_asks_for_the_webex_cpu_setting(self):
        bot = Bot.objects.create(name="Test Bot", project=self.project, meeting_url=WEBEX_URL)

        with patch("bots.models.os.getenv", side_effect=lambda key, default=None: {"WEBEX_AUDIO_AND_VIDEO_BOT_CPU_REQUEST": "6"}.get(key, default)):
            self.assertEqual(bot.cpu_request(), "6")


class WebexBotControllerTest(WebexBotTestCase):
    def setUp(self):
        super().setUp()
        self.bot = Bot.objects.create(name="Test Bot", project=self.project, meeting_url=WEBEX_URL)
        self.controller = object.__new__(BotController)  # the controller's constructor connects to the outside world
        self.controller.bot_in_db = self.bot
        self.controller.main_thread_executor = MainThreadExecutor()

    def test_audio_is_48khz_float_like_the_other_web_bots(self):
        self.assertEqual(self.controller.mixed_audio_sample_rate(), 48000)
        self.assertEqual(self.controller.get_per_participant_audio_sample_rate(), 48000)
        self.assertEqual(self.controller.get_audio_format(), GstreamerPipeline.AUDIO_FORMAT_FLOAT)

    def test_recording_is_done_by_the_browser_not_by_a_gstreamer_pipeline(self):
        self.controller.pipeline_configuration = SimpleNamespace(record_audio=True, record_video=True, rtmp_stream_audio=False, rtmp_stream_video=False)

        self.assertIs(self.controller.should_create_gstreamer_pipeline(), False)

    def test_the_webex_adapter_is_the_adapter_for_a_webex_meeting(self):
        self.controller.get_webex_bot_adapter = MagicMock()

        self.assertIs(self.controller.get_bot_adapter(), self.controller.get_webex_bot_adapter.return_value)

    def test_the_guest_token_is_created_from_the_project_credentials(self):
        self.add_credentials()

        with patch("bots.webex_bot_adapter.webex_guest_token.create_guest_token_from_refresh_token", return_value="guest-token") as create_guest_token:
            guest_token = self.controller.create_webex_guest_token()

        self.assertEqual(guest_token, "guest-token")
        create_guest_token.assert_called_once_with("client-id", "client-secret", "refresh-token", subject=f"attendee-{self.bot.object_id}", display_name="Test Bot")

    def test_creating_a_guest_token_without_credentials_is_an_authorization_error(self):
        with self.assertRaises(WebexAuthError):
            self.controller.create_webex_guest_token()
