from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from accounts.models import Organization
from bots.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.automatic_leave_utils import silence_detection_should_activate
from bots.bot_adapter import BotAdapter
from bots.bot_controller.bot_controller import BotController
from bots.models import Bot, BotLogEntry, BotLogEntryLevels, BotLogEntryTypes, Project
from bots.serializers import CreateBotSerializer
from bots.web_bot_adapter import WebBotAdapter


class SilenceDetectionShouldActivateTest(SimpleTestCase):
    def test_activates_after_silence_activate_after_seconds(self):
        configuration = AutomaticLeaveConfiguration(silence_activate_after_seconds=1200)

        self.assertFalse(silence_detection_should_activate(automatic_leave_configuration=configuration, seconds_since_joined=1200, get_number_of_participants_currently_in_meeting_excluding_other_bots=lambda: 1))
        self.assertTrue(silence_detection_should_activate(automatic_leave_configuration=configuration, seconds_since_joined=1201, get_number_of_participants_currently_in_meeting_excluding_other_bots=lambda: 1))

    def test_activates_once_enough_participants_are_in_the_meeting(self):
        configuration = AutomaticLeaveConfiguration(silence_activate_after_participant_count=2)

        # The count passed in includes our own bot
        self.assertFalse(silence_detection_should_activate(automatic_leave_configuration=configuration, seconds_since_joined=10, get_number_of_participants_currently_in_meeting_excluding_other_bots=lambda: 2))
        self.assertTrue(silence_detection_should_activate(automatic_leave_configuration=configuration, seconds_since_joined=10, get_number_of_participants_currently_in_meeting_excluding_other_bots=lambda: 3))

    def test_either_trigger_activates_whichever_comes_first(self):
        configuration = AutomaticLeaveConfiguration(silence_activate_after_seconds=1200, silence_activate_after_participant_count=1)

        self.assertTrue(silence_detection_should_activate(automatic_leave_configuration=configuration, seconds_since_joined=10, get_number_of_participants_currently_in_meeting_excluding_other_bots=lambda: 2))
        self.assertTrue(silence_detection_should_activate(automatic_leave_configuration=configuration, seconds_since_joined=1201, get_number_of_participants_currently_in_meeting_excluding_other_bots=lambda: 1))
        self.assertFalse(silence_detection_should_activate(automatic_leave_configuration=configuration, seconds_since_joined=10, get_number_of_participants_currently_in_meeting_excluding_other_bots=lambda: 1))

    def test_participant_count_is_ignored_and_not_counted_when_not_set(self):
        count_participants = MagicMock(return_value=50)

        self.assertFalse(silence_detection_should_activate(automatic_leave_configuration=AutomaticLeaveConfiguration(), seconds_since_joined=10, get_number_of_participants_currently_in_meeting_excluding_other_bots=count_participants))
        count_participants.assert_not_called()


class WebBotAdapterParticipantsCurrentlyInMeetingTest(SimpleTestCase):
    def test_counts_only_participants_still_in_the_meeting(self):
        adapter = object.__new__(WebBotAdapter)  # none of these tests need the adapter's constructor
        adapter.automatic_leave_configuration = AutomaticLeaveConfiguration()
        adapter.participants_info = {
            "bot": {"fullName": "Notetaker", "isCurrentUser": True, "active": True},
            "a": {"fullName": "Participant A", "isCurrentUser": False, "active": False},
            "b": {"fullName": "Participant B", "isCurrentUser": False, "active": True},
        }

        self.assertEqual(adapter.number_of_participants_currently_in_meeting_excluding_other_bots(), 2)


@patch("time.time")
class WebBotAdapterSilencePendingLogTest(SimpleTestCase):
    def setUp(self):
        self.adapter = object.__new__(WebBotAdapter)  # none of these tests need the adapter's constructor
        self.adapter.automatic_leave_configuration = AutomaticLeaveConfiguration(silence_timeout_seconds=480, silence_activate_after_participant_count=1, silence_pending_log_after_seconds=240)
        self.adapter.left_meeting = False
        self.adapter.cleaned_up = False
        self.adapter.check_domain_allow_list_violation = MagicMock()
        self.adapter.only_one_participant_in_meeting_at = None
        self.adapter.silence_detection_activated = False
        self.adapter.silence_pending_logged_for_last_audio_at = None
        self.adapter.joined_at = 1000
        self.adapter.last_audio_message_processed_time = None
        self.adapter.number_of_participants_currently_in_meeting_excluding_other_bots = MagicMock(return_value=1)
        self.adapter.send_message_callback = MagicMock()

    def check_at(self, mock_time, now):
        mock_time.return_value = now
        self.adapter.check_auto_leave_conditions()

    def sent_messages(self):
        return [call.args[0]["message"] for call in self.adapter.send_message_callback.call_args_list]

    def test_silence_detection_waits_for_the_first_participant(self, mock_time):
        self.check_at(mock_time, 1000 + 900)
        self.assertFalse(self.adapter.silence_detection_activated)

        self.adapter.number_of_participants_currently_in_meeting_excluding_other_bots.return_value = 2
        self.check_at(mock_time, 1000 + 901)
        self.assertTrue(self.adapter.silence_detection_activated)
        self.assertEqual(self.adapter.last_audio_message_processed_time, 1000 + 901)

    def test_logs_once_then_leaves(self, mock_time):
        self.adapter.number_of_participants_currently_in_meeting_excluding_other_bots.return_value = 2
        self.check_at(mock_time, 1000)

        self.check_at(mock_time, 1000 + 241)
        self.check_at(mock_time, 1000 + 300)
        self.assertEqual(self.sent_messages(), [BotAdapter.Messages.AUTO_LEAVE_SILENCE_PENDING])
        self.assertEqual(self.adapter.send_message_callback.call_args.args[0], {"message": BotAdapter.Messages.AUTO_LEAVE_SILENCE_PENDING, "seconds_of_silence": 240, "seconds_until_leave": 240})

        self.check_at(mock_time, 1000 + 481)
        self.assertEqual(self.sent_messages(), [BotAdapter.Messages.AUTO_LEAVE_SILENCE_PENDING, BotAdapter.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING])

    def test_logs_again_after_audio_resumes_and_stops(self, mock_time):
        self.adapter.number_of_participants_currently_in_meeting_excluding_other_bots.return_value = 2
        self.check_at(mock_time, 1000)
        self.check_at(mock_time, 1000 + 241)

        self.adapter.last_audio_message_processed_time = 1000 + 300
        self.check_at(mock_time, 1000 + 400)
        self.check_at(mock_time, 1000 + 541)

        self.assertEqual(self.sent_messages(), [BotAdapter.Messages.AUTO_LEAVE_SILENCE_PENDING, BotAdapter.Messages.AUTO_LEAVE_SILENCE_PENDING])

    def test_no_log_when_silence_pending_log_after_seconds_is_not_set(self, mock_time):
        self.adapter.automatic_leave_configuration = AutomaticLeaveConfiguration(silence_timeout_seconds=480, silence_activate_after_participant_count=1)
        self.adapter.number_of_participants_currently_in_meeting_excluding_other_bots.return_value = 2
        self.check_at(mock_time, 1000)

        self.check_at(mock_time, 1000 + 300)
        self.assertEqual(self.sent_messages(), [])


class SilencePendingLogEntryTest(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        project = Project.objects.create(name="Test Project", organization=organization)
        self.bot = Bot.objects.create(name="Test Bot", project=project, meeting_url="https://zoom.us/j/123456789")
        self.controller = object.__new__(BotController)  # the controller's constructor connects to the outside world
        self.controller.bot_in_db = self.bot

    @patch("bots.models.trigger_webhook")
    def test_silence_pending_creates_an_info_log_entry_and_fires_the_webhook(self, mock_trigger_webhook):
        self.controller.take_action_based_on_message_from_adapter({"message": BotAdapter.Messages.AUTO_LEAVE_SILENCE_PENDING, "seconds_of_silence": 240, "seconds_until_leave": 240})

        log_entry = BotLogEntry.objects.get(bot=self.bot)
        self.assertEqual(log_entry.level, BotLogEntryLevels.INFO)
        self.assertEqual(log_entry.entry_type, BotLogEntryTypes.AUTO_LEAVE_SILENCE_PENDING)
        self.assertEqual(log_entry.message, "No audio detected for 240 seconds. Bot will leave the meeting in 240 seconds if no audio is detected.")
        self.assertEqual(mock_trigger_webhook.call_args.kwargs["payload"]["entry_type"], "auto_leave_silence_pending")


class SilencePendingLogValidationTest(SimpleTestCase):
    def test_silence_pending_log_after_seconds_must_be_less_than_silence_timeout_seconds(self):
        from rest_framework.exceptions import ValidationError

        serializer = CreateBotSerializer()

        with self.assertRaises(ValidationError):
            serializer.validate_automatic_leave_settings({"silence_timeout_seconds": 480, "silence_pending_log_after_seconds": 480})

        validated = serializer.validate_automatic_leave_settings({"silence_timeout_seconds": 480, "silence_pending_log_after_seconds": 240, "silence_activate_after_participant_count": 1})
        self.assertEqual(validated["silence_pending_log_after_seconds"], 240)
        self.assertEqual(validated["silence_activate_after_participant_count"], 1)

    def test_new_settings_are_off_by_default(self):
        validated = CreateBotSerializer().validate_automatic_leave_settings({})

        self.assertIsNone(validated["silence_pending_log_after_seconds"])
        self.assertIsNone(validated["silence_activate_after_participant_count"])
        self.assertEqual(validated["silence_timeout_seconds"], 600)
        self.assertEqual(validated["silence_activate_after_seconds"], 1200)
