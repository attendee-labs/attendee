from unittest.mock import patch

from django.test import TestCase

from bots.bot_adapter import BotAdapter
from bots.bot_controller import BotController
from bots.models import (
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Organization,
    Project,
)


class MeetingEndedBeforeBotWasAdmittedTests(TestCase):
    """Covers the adapter reporting that the meeting ended while the bot was never admitted.

    Every adapter funnels its meeting-ended signal into BotAdapter.Messages.MEETING_ENDED
    (Teams handleConversationEnd, the Zoom SDK's MEETING_STATUS_ENDED, Google Meet's
    removed_from_meeting), so a meeting that ends while the bot is still knocking arrives
    here indistinguishable from a meeting that ends after the bot recorded it.
    """

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://zoom.us/j/123456789",
            name="Test Bot",
        )

    def send_meeting_ended_from_adapter(self):
        # cleanup() tears down GStreamer/Chrome and flush_utterances() drains managers that
        # only exist once run() has been called, neither of which is what these tests cover.
        with patch.object(BotController, "cleanup"), patch.object(BotController, "flush_utterances"):
            BotController(self.bot.id).take_action_based_on_message_from_adapter({"message": BotAdapter.Messages.MEETING_ENDED})

        self.bot.refresh_from_db()
        return self.bot.bot_events.order_by("created_at").last()

    def test_meeting_ending_while_bot_waits_in_the_waiting_room_is_reported_as_could_not_join(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.BOT_PUT_IN_WAITING_ROOM)

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(last_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED)
        self.assertEqual(last_event.old_state, BotStates.WAITING_ROOM)
        self.assertEqual(last_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

    def test_meeting_ending_while_bot_is_still_joining_is_reported_as_could_not_join(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(last_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED)
        self.assertEqual(last_event.old_state, BotStates.JOINING)
        self.assertEqual(last_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

    def test_meeting_ending_after_the_bot_was_admitted_is_still_reported_as_meeting_ended(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.BOT_JOINED_MEETING)

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.MEETING_ENDED)
        self.assertIsNone(last_event.event_sub_type)
        self.assertEqual(last_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(last_event.new_state, BotStates.POST_PROCESSING)

    def test_bot_asked_to_leave_from_the_waiting_room_is_still_reported_as_left_meeting(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.BOT_PUT_IN_WAITING_ROOM)
        BotEventManager.create_event(
            bot=self.bot,
            event_type=BotEventTypes.LEAVE_REQUESTED,
            event_sub_type=BotEventSubTypes.LEAVE_REQUESTED_USER_REQUESTED,
        )

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
        self.assertEqual(last_event.old_state, BotStates.LEAVING)
        self.assertEqual(last_event.new_state, BotStates.POST_PROCESSING)
