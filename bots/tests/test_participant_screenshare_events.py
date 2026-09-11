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
        adapter.add_participant_event_callback = MagicMock()
        return adapter

    def test_start_message_becomes_screenshare_start_event(self):
        adapter = self.build_adapter()

        adapter.handle_participant_screenshare_start_stop_event({"type": "ParticipantScreenshareStartStopEvent", "participantId": "device-1", "isScreenshareStart": True, "timestamp": 1723456789000})

        adapter.add_participant_event_callback.assert_called_once_with({"participant_uuid": "device-1", "event_type": ParticipantEventTypes.SCREENSHARE_START, "event_data": {"source": "screenshare"}, "timestamp_ms": 1723456789000})

    def test_stop_message_becomes_screenshare_stop_event(self):
        adapter = self.build_adapter()

        adapter.handle_participant_screenshare_start_stop_event({"type": "ParticipantScreenshareStartStopEvent", "participantId": "device-1", "isScreenshareStart": False, "timestamp": 1723456799000.0})

        adapter.add_participant_event_callback.assert_called_once_with({"participant_uuid": "device-1", "event_type": ParticipantEventTypes.SCREENSHARE_STOP, "event_data": {"source": "screenshare"}, "timestamp_ms": 1723456799000})

    def test_websocket_dispatches_screenshare_messages(self):
        adapter = self.build_adapter()
        message = {"type": "ParticipantScreenshareStartStopEvent", "participantId": "device-1", "isScreenshareStart": True, "timestamp": 1723456789000}
        # The payload frames JSON messages as a little-endian type prefix of 1 followed by the UTF-8 body
        fake_websocket = [(1).to_bytes(4, byteorder="little") + json.dumps(message).encode("utf-8")]

        adapter.handle_websocket(fake_websocket)

        adapter.add_participant_event_callback.assert_called_once_with({"participant_uuid": "device-1", "event_type": ParticipantEventTypes.SCREENSHARE_START, "event_data": {"source": "screenshare"}, "timestamp_ms": 1723456789000})


@tag("zoom_tests")
class ZoomBotAdapterScreenshareEventTest(SimpleTestCase):
    """Screenshare events follow the active sharer the adapter already tracks for the video pipeline."""

    SHARE_BEGIN = 101
    VIEW_OTHER_SHARING = 102
    SHARE_END = 103
    NOW_MS = 1723456789000

    def setUp(self):
        fake_sdk = SimpleNamespace(Sharing_Other_Share_Begin=self.SHARE_BEGIN, Sharing_View_Other_Sharing=self.VIEW_OTHER_SHARING, Sharing_Other_Share_End=self.SHARE_END)
        sdk_patcher = patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", fake_sdk)
        sdk_patcher.start()
        self.addCleanup(sdk_patcher.stop)

        time_patcher = patch("bots.zoom_bot_adapter.zoom_bot_adapter.time.time", return_value=self.NOW_MS / 1000)
        time_patcher.start()
        self.addCleanup(time_patcher.stop)

    def build_adapter(self, record_screenshare_events=True):
        adapter = ZoomBotAdapter.__new__(ZoomBotAdapter)
        adapter.record_participant_screenshare_start_stop_events = record_screenshare_events
        adapter.active_sharer_id = None
        adapter.active_sharer_source_id = None
        adapter.add_participant_event_callback = MagicMock()
        adapter.set_video_input_manager_based_on_state = MagicMock()
        adapter.meeting_sharing_controller = MagicMock()
        return adapter

    def sharing_info(self, user_id, status, share_source_id=None):
        return SimpleNamespace(userid=user_id, status=status, shareSourceID=share_source_id)

    def start_event(self, user_id, share_source_id):
        return call({"participant_uuid": user_id, "event_type": ParticipantEventTypes.SCREENSHARE_START, "event_data": {"source": "screenshare", "share_source_id": share_source_id}, "timestamp_ms": self.NOW_MS})

    def stop_event(self, user_id):
        return call({"participant_uuid": user_id, "event_type": ParticipantEventTypes.SCREENSHARE_STOP, "event_data": {"source": "screenshare"}, "timestamp_ms": self.NOW_MS})

    def test_share_begin_emits_start_with_share_source_id(self):
        adapter = self.build_adapter()

        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=42))

        self.assertEqual(adapter.add_participant_event_callback.call_args_list, [self.start_event(2, 42)])
        self.assertEqual((adapter.active_sharer_id, adapter.active_sharer_source_id), (2, 42))
        adapter.set_video_input_manager_based_on_state.assert_called_once()

    def test_repeated_status_for_the_same_share_is_idempotent(self):
        adapter = self.build_adapter()

        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=42))
        adapter.on_sharing_status_callback(self.sharing_info(2, self.VIEW_OTHER_SHARING, share_source_id=42))
        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=42))

        self.assertEqual(adapter.add_participant_event_callback.call_args_list, [self.start_event(2, 42)])
        adapter.set_video_input_manager_based_on_state.assert_called_once()

    def test_share_end_emits_stop(self):
        adapter = self.build_adapter()

        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=42))
        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_END))

        self.assertEqual(adapter.add_participant_event_callback.call_args_list, [self.start_event(2, 42), self.stop_event(2)])
        self.assertEqual((adapter.active_sharer_id, adapter.active_sharer_source_id), (None, None))

    def test_share_end_without_an_active_share_emits_nothing(self):
        adapter = self.build_adapter()

        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_END))

        adapter.add_participant_event_callback.assert_not_called()
        adapter.set_video_input_manager_based_on_state.assert_not_called()

    def test_sharer_switch_emits_stop_for_old_sharer_then_start_for_new_sharer(self):
        adapter = self.build_adapter()

        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=42))
        adapter.on_sharing_status_callback(self.sharing_info(3, self.SHARE_BEGIN, share_source_id=77))

        self.assertEqual(adapter.add_participant_event_callback.call_args_list, [self.start_event(2, 42), self.stop_event(2), self.start_event(3, 77)])

    def test_source_change_for_the_same_sharer_emits_no_event_but_updates_video(self):
        adapter = self.build_adapter()

        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=42))
        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=43))

        self.assertEqual(adapter.add_participant_event_callback.call_args_list, [self.start_event(2, 42)])
        self.assertEqual(adapter.active_sharer_source_id, 43)
        self.assertEqual(adapter.set_video_input_manager_based_on_state.call_count, 2)

    def test_disabled_setting_tracks_sharer_without_emitting_events(self):
        adapter = self.build_adapter(record_screenshare_events=False)

        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_BEGIN, share_source_id=42))
        adapter.on_sharing_status_callback(self.sharing_info(2, self.SHARE_END))

        adapter.add_participant_event_callback.assert_not_called()
        self.assertEqual(adapter.set_video_input_manager_based_on_state.call_count, 2)

    def test_share_in_progress_when_video_starts_emits_start_once(self):
        adapter = self.build_adapter()
        adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = [2]
        adapter.meeting_sharing_controller.GetSharingSourceInfoList.return_value = [SimpleNamespace(userid=2, shareSourceID=42)]

        adapter.set_up_video_input_manager()
        adapter.set_up_video_input_manager()

        self.assertEqual(adapter.add_participant_event_callback.call_args_list, [self.start_event(2, 42)])
        self.assertEqual((adapter.active_sharer_id, adapter.active_sharer_source_id), (2, 42))
        self.assertEqual(adapter.set_video_input_manager_based_on_state.call_count, 2)

    def test_no_share_in_progress_when_video_starts_emits_nothing(self):
        adapter = self.build_adapter()
        adapter.meeting_sharing_controller.GetViewableSharingUserList.return_value = []

        adapter.set_up_video_input_manager()

        adapter.add_participant_event_callback.assert_not_called()
        adapter.set_video_input_manager_based_on_state.assert_called_once()


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

        controller.add_participant_event(self.participant_event(ParticipantEventTypes.SCREENSHARE_START, {"source": "screenshare", "share_source_id": 42}))

        [participant_event] = ParticipantEvent.objects.filter(participant__bot=self.bot)
        self.assertEqual(participant_event.event_type, ParticipantEventTypes.SCREENSHARE_START)
        self.assertEqual(participant_event.event_data, {"source": "screenshare", "share_source_id": 42})
        self.assertEqual(participant_event.participant.uuid, "user-1")

        mock_trigger_webhook.assert_called_once()
        kwargs = mock_trigger_webhook.call_args.kwargs
        self.assertEqual(kwargs["webhook_trigger_type"], WebhookTriggerTypes.PARTICIPANT_EVENTS_SCREENSHARE_START_STOP)
        self.assertEqual(kwargs["bot"], self.bot)
        self.assertEqual(kwargs["payload"]["event_type"], "screenshare_start")
        self.assertEqual(kwargs["payload"]["event_data"], {"source": "screenshare", "share_source_id": 42})
        self.assertEqual(kwargs["payload"]["participant_name"], "Jane Doe")

    @patch("bots.bot_controller.bot_controller.trigger_webhook")
    def test_screenshare_stop_is_routed_to_the_screenshare_trigger(self, mock_trigger_webhook):
        controller = self.build_controller()

        controller.add_participant_event(self.participant_event(ParticipantEventTypes.SCREENSHARE_STOP, {"source": "screenshare"}))

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

        controller.add_participant_event(self.participant_event(ParticipantEventTypes.SCREENSHARE_START, {"source": "screenshare"}))

        self.assertEqual(ParticipantEvent.objects.filter(participant__bot=self.bot, event_type=ParticipantEventTypes.SCREENSHARE_START).count(), 1)
        mock_trigger_webhook.assert_not_called()
