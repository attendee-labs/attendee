import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from django.test import SimpleTestCase, TestCase, tag
from rest_framework import serializers

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.models import Bot, BotStates, ParticipantEvent, ParticipantEventTypes, Project, WebhookTriggerTypes
from bots.serializers import BOT_RECORDING_SETTINGS_DEFAULT_VALUES, CreateBotSerializer, get_webhook_trigger_enum
from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter
from bots.zoom_bot_adapter.zoom_bot_adapter import ZoomBotAdapter

SCREENSHARE_SETTING = "record_participant_screenshare_start_stop_events"
SCREENSHARE_TRIGGER_CODE = "participant_events.screenshare_start_stop"


class ParticipantScreenshareEventCodesTest(SimpleTestCase):
    def test_screenshare_event_types_map_to_api_codes(self):
        self.assertEqual(ParticipantEventTypes.type_to_api_code(ParticipantEventTypes.SCREENSHARE_START), "screenshare_start")
        self.assertEqual(ParticipantEventTypes.type_to_api_code(ParticipantEventTypes.SCREENSHARE_STOP), "screenshare_stop")

    def test_existing_event_type_codes_are_unchanged(self):
        self.assertEqual(ParticipantEventTypes.type_to_api_code(ParticipantEventTypes.SPEECH_START), "speech_start")
        self.assertEqual(ParticipantEventTypes.type_to_api_code(ParticipantEventTypes.SPEECH_STOP), "speech_stop")

    def test_screenshare_trigger_maps_to_api_code_and_back(self):
        self.assertEqual(WebhookTriggerTypes.trigger_type_to_api_code(WebhookTriggerTypes.PARTICIPANT_EVENTS_SCREENSHARE_START_STOP), SCREENSHARE_TRIGGER_CODE)
        self.assertEqual(WebhookTriggerTypes.api_code_to_trigger_type(SCREENSHARE_TRIGGER_CODE), WebhookTriggerTypes.PARTICIPANT_EVENTS_SCREENSHARE_START_STOP)

    def test_screenshare_trigger_is_offered_to_webhook_subscribers(self):
        self.assertIn(SCREENSHARE_TRIGGER_CODE, get_webhook_trigger_enum())


class RecordParticipantScreenshareStartStopEventsSettingTest(SimpleTestCase):
    def validate(self, value):
        return CreateBotSerializer().validate_recording_settings(value)

    def test_setting_defaults_to_false(self):
        self.assertIs(BOT_RECORDING_SETTINGS_DEFAULT_VALUES[SCREENSHARE_SETTING], False)
        self.assertIs(self.validate({"format": "mp4"})[SCREENSHARE_SETTING], False)

    def test_setting_can_be_enabled(self):
        self.assertIs(self.validate({SCREENSHARE_SETTING: True})[SCREENSHARE_SETTING], True)

    def test_setting_rejects_non_boolean_values(self):
        with self.assertRaises(serializers.ValidationError):
            self.validate({SCREENSHARE_SETTING: "yes"})

    def test_bot_accessor_defaults_to_false(self):
        self.assertIs(Bot(settings={}).record_participant_screenshare_start_stop_events(), False)
        self.assertIs(Bot(settings={"recording_settings": None}).record_participant_screenshare_start_stop_events(), False)
        self.assertIs(Bot(settings={"recording_settings": {"format": "mp4"}}).record_participant_screenshare_start_stop_events(), False)

    def test_bot_accessor_reads_enabled_setting(self):
        self.assertIs(Bot(settings={"recording_settings": {SCREENSHARE_SETTING: True}}).record_participant_screenshare_start_stop_events(), True)


class WebBotAdapterScreenshareEventTest(SimpleTestCase):
    """The websocket message that the Google Meet and Teams payloads send is turned into a participant event."""

    def build_adapter(self):
        adapter = WebBotAdapter.__new__(WebBotAdapter)
        adapter.record_participant_screenshare_start_stop_events = True
        adapter.add_participant_event_callback = MagicMock()
        return adapter

    def test_start_message_becomes_screenshare_start_event(self):
        adapter = self.build_adapter()

        adapter.handle_participant_screenshare_start_stop_event({"type": "ParticipantScreenshareStartStopEvent", "participantId": "device-1", "isScreenshareStart": True, "timestamp": 1723456789000})

        adapter.add_participant_event_callback.assert_called_once_with({"participant_uuid": "device-1", "event_type": ParticipantEventTypes.SCREENSHARE_START, "event_data": {}, "timestamp_ms": 1723456789000})

    def test_stop_message_becomes_screenshare_stop_event(self):
        adapter = self.build_adapter()

        adapter.handle_participant_screenshare_start_stop_event({"type": "ParticipantScreenshareStartStopEvent", "participantId": "device-1", "isScreenshareStart": False, "timestamp": 1723456799000.0})

        adapter.add_participant_event_callback.assert_called_once_with({"participant_uuid": "device-1", "event_type": ParticipantEventTypes.SCREENSHARE_STOP, "event_data": {}, "timestamp_ms": 1723456799000})

    def test_websocket_dispatches_screenshare_messages(self):
        adapter = self.build_adapter()
        message = {"type": "ParticipantScreenshareStartStopEvent", "participantId": "device-1", "isScreenshareStart": True, "timestamp": 1723456789000}
        # The payload frames JSON messages as a little-endian type prefix of 1 followed by the UTF-8 body
        fake_websocket = [(1).to_bytes(4, byteorder="little") + json.dumps(message).encode("utf-8")]

        adapter.handle_websocket(fake_websocket)

        adapter.add_participant_event_callback.assert_called_once_with({"participant_uuid": "device-1", "event_type": ParticipantEventTypes.SCREENSHARE_START, "event_data": {}, "timestamp_ms": 1723456789000})

    def test_disabled_setting_ignores_browser_event(self):
        adapter = self.build_adapter()
        adapter.record_participant_screenshare_start_stop_events = False
        adapter.handle_participant_screenshare_start_stop_event({"participantId": "device-1", "isScreenshareStart": True, "timestamp": 1723456789000})
        adapter.add_participant_event_callback.assert_not_called()


@tag("zoom_tests")
class ZoomBotAdapterScreenshareEventTest(SimpleTestCase):
    SHARE_BEGIN, VIEW_OTHER_SHARING, SHARE_END, PAUSE, RESUME, AUDIO_BEGIN, AUDIO_END = range(101, 108)
    VIDEO_CONTENT, AUDIO_CONTENT, UNKNOWN_CONTENT = range(201, 204)
    NOW_MS = 1723456789000

    def setUp(self):
        fake_sdk = SimpleNamespace(Sharing_Other_Share_Begin=self.SHARE_BEGIN, Sharing_View_Other_Sharing=self.VIEW_OTHER_SHARING, Sharing_Other_Share_End=self.SHARE_END, Sharing_Pause=self.PAUSE, Sharing_Resume=self.RESUME, SHARE_TYPE_COMPUTER_AUDIO=self.AUDIO_CONTENT)
        sdk_patcher = patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", fake_sdk)
        sdk_patcher.start()
        self.addCleanup(sdk_patcher.stop)
        time_patcher = patch("bots.zoom_bot_adapter.zoom_bot_adapter.time.time", return_value=self.NOW_MS / 1000)
        time_patcher.start()
        self.addCleanup(time_patcher.stop)
        self.adapter = ZoomBotAdapter.__new__(ZoomBotAdapter)
        self.adapter.record_participant_screenshare_start_stop_events = True
        self.adapter.participant_screenshare_sources = {}
        self.adapter.active_sharer_id = None
        self.adapter.active_sharer_source_id = None
        self.adapter.add_participant_event_callback = MagicMock()
        self.adapter.set_video_input_manager_based_on_state = MagicMock()
        self.adapter.update_only_one_participant_in_meeting_at = MagicMock()
        self.adapter.meeting_sharing_controller = MagicMock()

    def source(self, user_id, status, source_id=42, content_type=VIDEO_CONTENT):
        return SimpleNamespace(userid=user_id, status=status, shareSourceID=source_id, contentType=content_type)

    def status(self, user_id, status, source_id=42):
        self.adapter.on_sharing_status_callback(self.source(user_id, status, source_id))

    def event(self, user_id, event_type):
        return call({"participant_uuid": user_id, "event_type": event_type, "event_data": {}, "timestamp_ms": self.NOW_MS})

    def assert_events(self, *events):
        self.assertEqual(self.adapter.add_participant_event_callback.call_args_list, [self.event(*event) for event in events])

    def test_begin_view_and_repeat_begin_emit_one_start(self):
        for status in [self.SHARE_BEGIN, self.VIEW_OTHER_SHARING, self.SHARE_BEGIN]:
            self.status(2, status)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))

    def test_explicit_end_emits_stop_once(self):
        self.status(2, self.SHARE_BEGIN)
        self.status(2, self.SHARE_END)
        self.status(2, self.SHARE_END)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (2, ParticipantEventTypes.SCREENSHARE_STOP))

    def test_pause_and_resume_do_not_end_the_session(self):
        for status in [self.SHARE_BEGIN, self.PAUSE, self.RESUME]:
            self.status(2, status)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))
        self.status(2, self.SHARE_END)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (2, ParticipantEventTypes.SCREENSHARE_STOP))

    def test_simultaneous_sharer_and_view_switch_do_not_stop_first_sharer(self):
        self.status(2, self.SHARE_BEGIN)
        self.status(3, self.SHARE_BEGIN, 77)
        self.status(3, self.VIEW_OTHER_SHARING, 77)
        self.status(2, self.SHARE_END)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (3, ParticipantEventTypes.SCREENSHARE_START), (2, ParticipantEventTypes.SCREENSHARE_STOP))
        self.assertEqual(self.adapter.participant_screenshare_sources, {3: {77}})

    def test_last_source_end_stops_participant(self):
        self.status(2, self.SHARE_BEGIN, 42)
        self.status(2, self.SHARE_BEGIN, 43)
        self.status(2, self.SHARE_END, 42)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))
        self.status(2, self.SHARE_END, 43)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (2, ParticipantEventTypes.SCREENSHARE_STOP))

    def test_unknown_source_end_does_not_stop_known_source(self):
        self.status(2, self.SHARE_BEGIN, 42)
        self.status(2, self.SHARE_END, 99)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))

    def test_unrelated_and_audio_only_status_do_not_end_share(self):
        self.status(2, self.SHARE_BEGIN)
        self.status(3, self.AUDIO_BEGIN)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))

    def test_participant_leave_closes_all_their_sources(self):
        self.status(2, self.SHARE_BEGIN, 42)
        self.status(2, self.SHARE_BEGIN, 43)
        self.adapter.on_user_left_callback([2], None)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (2, ParticipantEventTypes.SCREENSHARE_STOP), (2, ParticipantEventTypes.LEAVE))
        self.assertEqual(self.adapter.participant_screenshare_sources, {})

    def test_stop_and_restart_is_a_new_session(self):
        for status in [self.SHARE_BEGIN, self.SHARE_END, self.SHARE_BEGIN]:
            self.status(2, status)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (2, ParticipantEventTypes.SCREENSHARE_STOP), (2, ParticipantEventTypes.SCREENSHARE_START))

    def test_initial_snapshot_includes_all_sharers_without_duplicates(self):
        self.adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = [2, 3]
        self.adapter.meeting_sharing_controller.GetSharingSourceInfoList.side_effect = lambda user: [self.source(user, self.SHARE_BEGIN, user * 10)]
        self.adapter.observe_initial_screenshares()
        self.adapter.observe_initial_screenshares()
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (3, ParticipantEventTypes.SCREENSHARE_START))

    def test_unknown_initial_snapshot_does_not_synthesize_a_stop(self):
        self.status(2, self.SHARE_BEGIN)
        self.adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = None
        self.adapter.observe_initial_screenshares()
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))

    def test_initial_snapshot_uses_the_same_source_classification_as_callbacks(self):
        self.adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = [2]
        self.adapter.meeting_sharing_controller.GetSharingSourceInfoList.return_value = [
            self.source(2, self.AUDIO_BEGIN, 40, self.AUDIO_CONTENT),
            self.source(2, self.VIEW_OTHER_SHARING, 41, self.AUDIO_CONTENT),
            self.source(2, self.SHARE_END, 42),
            self.source(2, self.PAUSE, 43, self.UNKNOWN_CONTENT),
        ]
        self.adapter.observe_initial_screenshares()
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))
        self.assertEqual(self.adapter.participant_screenshare_sources, {2: {43}})
        self.status(2, self.SHARE_END, 43)
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START), (2, ParticipantEventTypes.SCREENSHARE_STOP))

    def test_audio_only_initial_snapshot_does_not_open_a_session(self):
        self.adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = [2]
        self.adapter.meeting_sharing_controller.GetSharingSourceInfoList.return_value = [self.source(2, self.AUDIO_BEGIN, content_type=self.AUDIO_CONTENT)]
        self.adapter.observe_initial_screenshares()
        self.status(2, self.AUDIO_END)
        self.assert_events()

    def test_recording_setup_retries_initial_discovery(self):
        self.adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = []
        self.adapter.observe_initial_screenshares()
        self.adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = [2]
        self.adapter.meeting_sharing_controller.GetSharingSourceInfoList.return_value = [self.source(2, self.PAUSE)]
        self.adapter.set_up_video_input_manager()
        self.adapter.set_up_video_input_manager()
        self.assert_events((2, ParticipantEventTypes.SCREENSHARE_START))

    def test_disabled_setting_neither_queries_nor_emits_sharing_events(self):
        self.adapter.record_participant_screenshare_start_stop_events = False
        self.adapter.observe_initial_screenshares()
        self.status(2, self.SHARE_BEGIN)
        self.status(2, self.SHARE_END)
        self.assert_events()
        self.adapter.meeting_sharing_controller.GetViewableSharingUserList.assert_not_called()
        self.assertEqual(self.adapter.set_video_input_manager_based_on_state.call_count, 2)


class BotControllerScreenshareWebhookRoutingTest(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/123", state=BotStates.JOINED_RECORDING)

    def build_controller(self, participant_is_the_bot=False):
        controller = BotController.__new__(BotController)
        controller.bot_in_db = self.bot
        controller.room_sync_client = None
        controller.adapter = MagicMock()
        controller.adapter.get_participant.return_value = {
            "participant_uuid": "user-1",
            "participant_user_uuid": None,
            "participant_full_name": "Jane Doe",
            "participant_is_the_bot": participant_is_the_bot,
            "participant_is_host": False,
        }
        return controller

    def participant_event(self, event_type, event_data):
        return {"participant_uuid": "user-1", "event_type": event_type, "event_data": event_data, "timestamp_ms": 1723456789000}

    @patch("bots.bot_controller.bot_controller.trigger_webhook")
    def test_screenshare_start_is_persisted_and_routed_to_the_screenshare_trigger(self, mock_trigger_webhook):
        controller = self.build_controller()

        controller.add_participant_event(self.participant_event(ParticipantEventTypes.SCREENSHARE_START, {}))

        [participant_event] = ParticipantEvent.objects.filter(participant__bot=self.bot)
        self.assertEqual(participant_event.event_type, ParticipantEventTypes.SCREENSHARE_START)
        self.assertEqual(participant_event.event_data, {})
        self.assertEqual(participant_event.participant.uuid, "user-1")

        mock_trigger_webhook.assert_called_once()
        kwargs = mock_trigger_webhook.call_args.kwargs
        self.assertEqual(kwargs["webhook_trigger_type"], WebhookTriggerTypes.PARTICIPANT_EVENTS_SCREENSHARE_START_STOP)
        self.assertEqual(kwargs["bot"], self.bot)
        self.assertEqual(kwargs["payload"]["event_type"], "screenshare_start")
        self.assertEqual(kwargs["payload"]["event_data"], {})
        self.assertEqual(kwargs["payload"]["participant_name"], "Jane Doe")

    @patch("bots.bot_controller.bot_controller.trigger_webhook")
    def test_screenshare_stop_is_routed_to_the_screenshare_trigger(self, mock_trigger_webhook):
        controller = self.build_controller()

        controller.add_participant_event(self.participant_event(ParticipantEventTypes.SCREENSHARE_STOP, {}))

        kwargs = mock_trigger_webhook.call_args.kwargs
        self.assertEqual(kwargs["webhook_trigger_type"], WebhookTriggerTypes.PARTICIPANT_EVENTS_SCREENSHARE_START_STOP)
        self.assertEqual(kwargs["payload"]["event_type"], "screenshare_stop")

    @patch("bots.bot_controller.bot_controller.trigger_webhook")
    def test_speech_events_still_route_to_the_speech_trigger(self, mock_trigger_webhook):
        controller = self.build_controller()

        controller.add_participant_event(self.participant_event(ParticipantEventTypes.SPEECH_START, {}))

        self.assertEqual(mock_trigger_webhook.call_args.kwargs["webhook_trigger_type"], WebhookTriggerTypes.PARTICIPANT_EVENTS_SPEECH_START_STOP)

    @patch("bots.bot_controller.bot_controller.trigger_webhook")
    def test_screenshare_by_the_bot_itself_is_persisted_without_a_webhook(self, mock_trigger_webhook):
        controller = self.build_controller(participant_is_the_bot=True)

        controller.add_participant_event(self.participant_event(ParticipantEventTypes.SCREENSHARE_START, {}))

        self.assertEqual(ParticipantEvent.objects.filter(participant__bot=self.bot, event_type=ParticipantEventTypes.SCREENSHARE_START).count(), 1)
        mock_trigger_webhook.assert_not_called()
