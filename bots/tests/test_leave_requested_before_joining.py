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


class LeaveRequestedBeforeBotJoinedTests(TestCase):
    """Covers a leave request that arrives while the bot is still joining or waiting to be admitted.

    The adapter reports the leave the same way it reports a meeting ending, via
    BotAdapter.Messages.MEETING_ENDED, so the only way to tell that the bot never actually
    made it into the meeting is the state it was in when the leave was requested.
    """

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://zoom.us/j/123456789",
            name="Test Bot",
        )

    def request_leave(self):
        BotEventManager.create_event(
            bot=self.bot,
            event_type=BotEventTypes.LEAVE_REQUESTED,
            event_sub_type=BotEventSubTypes.LEAVE_REQUESTED_USER_REQUESTED,
        )

    def send_meeting_ended_from_adapter(self):
        # cleanup() tears down GStreamer/Chrome and flush_utterances() drains managers that
        # only exist once run() has been called, neither of which is what these tests cover.
        with patch.object(BotController, "cleanup"), patch.object(BotController, "flush_utterances"):
            BotController(self.bot.id).take_action_based_on_message_from_adapter({"message": BotAdapter.Messages.MEETING_ENDED})

        self.bot.refresh_from_db()
        return self.bot.bot_events.order_by("created_at").last()

    def test_leave_requested_from_the_waiting_room_is_reported_as_could_not_join(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.BOT_PUT_IN_WAITING_ROOM)
        self.request_leave()

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(last_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_LEAVE_REQUESTED_BEFORE_JOINING)
        self.assertEqual(last_event.old_state, BotStates.LEAVING)
        self.assertEqual(last_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

    def test_leave_requested_while_still_joining_is_reported_as_could_not_join(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)
        self.request_leave()

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(last_event.event_sub_type, BotEventSubTypes.COULD_NOT_JOIN_MEETING_LEAVE_REQUESTED_BEFORE_JOINING)
        self.assertEqual(last_event.old_state, BotStates.LEAVING)
        self.assertEqual(last_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

    def test_leave_requested_after_the_bot_joined_is_still_reported_as_left_meeting(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.BOT_JOINED_MEETING)
        self.request_leave()

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
        self.assertIsNone(last_event.event_sub_type)
        self.assertEqual(last_event.old_state, BotStates.LEAVING)
        self.assertEqual(last_event.new_state, BotStates.POST_PROCESSING)

    def test_leave_requested_after_the_bot_was_admitted_and_put_in_a_breakout_room_is_still_reported_as_left_meeting(self):
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.BOT_JOINED_MEETING)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.BOT_BEGAN_JOINING_BREAKOUT_ROOM)
        self.request_leave()

        last_event = self.send_meeting_ended_from_adapter()

        self.assertEqual(last_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
        self.assertEqual(last_event.old_state, BotStates.LEAVING)
        self.assertEqual(last_event.new_state, BotStates.POST_PROCESSING)
