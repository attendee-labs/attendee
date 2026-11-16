from unittest.mock import MagicMock

from django.test import SimpleTestCase

from bots.bot_adapter import BotAdapter
from bots.models import ParticipantEventTypes


class TestParticipantMediaState(SimpleTestCase):
    def setUp(self):
        self.adapter = BotAdapter()
        self.adapter.add_participant_event_callback = MagicMock()

    def event_types(self):
        return [call.args[0]["event_type"] for call in self.adapter.add_participant_event_callback.call_args_list]

    def test_no_events_when_muted_and_webcam_off_whole_time(self):
        self.adapter.update_participant_media_state("p1", is_unmuted=False, is_webcam_on=False)
        self.adapter.update_participant_media_state("p1", is_unmuted=False, is_webcam_on=False)
        self.adapter.update_participant_media_state("p1", is_unmuted=False, is_webcam_on=False)
        self.assertEqual(self.event_types(), [])

    def test_events_when_unmuted_and_webcam_on_when_first_seen(self):
        self.adapter.update_participant_media_state("p1", is_unmuted=True, is_webcam_on=True)
        self.assertEqual(self.event_types(), [ParticipantEventTypes.UNMUTE, ParticipantEventTypes.WEBCAM_ON])
        self.assertEqual(self.adapter.add_participant_event_callback.call_args_list[0].args[0]["participant_uuid"], "p1")

    def test_events_only_on_change(self):
        self.adapter.update_participant_media_state("p1", is_unmuted=True)
        self.adapter.update_participant_media_state("p1", is_unmuted=True)
        self.adapter.update_participant_media_state("p1", is_unmuted=False, is_webcam_on=None)
        self.adapter.update_participant_media_state("p1", is_webcam_on=True)
        self.adapter.update_participant_media_state("p1", is_unmuted=None, is_webcam_on=False)
        self.assertEqual(self.event_types(), [ParticipantEventTypes.UNMUTE, ParticipantEventTypes.MUTE, ParticipantEventTypes.WEBCAM_ON, ParticipantEventTypes.WEBCAM_OFF])

    def test_participants_are_tracked_separately(self):
        self.adapter.update_participant_media_state("p1", is_unmuted=True)
        self.adapter.update_participant_media_state("p2", is_unmuted=True)
        self.assertEqual(self.event_types(), [ParticipantEventTypes.UNMUTE, ParticipantEventTypes.UNMUTE])
