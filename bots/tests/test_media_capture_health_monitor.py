import json
from unittest.mock import MagicMock, patch

import numpy as np
from django.test import SimpleTestCase, TestCase

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.bot_controller.media_capture_health_monitor import CaptionsWithoutPerParticipantAudioCheck, MediaCaptureHealthMonitor, MediaCaptureIssue, MediaCaptureIssueType
from bots.bot_controller.pipeline_configuration import PipelineConfiguration
from bots.media_capture_health_signals import MediaCaptureHealthSignal, MediaCaptureHealthSignalDebouncer
from bots.models import Bot, BotLogEntry, BotLogEntryLevels, BotLogEntryTypes, Project
from bots.web_bot_adapter import WebBotAdapter

CAPTION = MediaCaptureHealthSignal.CAPTION_ACTIVITY
AUDIO = MediaCaptureHealthSignal.PER_PARTICIPANT_AUDIO


class MediaCaptureHealthSignalDebouncerTest(SimpleTestCase):
    def test_lets_a_signal_through_at_most_once_per_interval(self):
        debouncer = MediaCaptureHealthSignalDebouncer(180)

        self.assertTrue(debouncer.should_emit(CAPTION, 1000))
        self.assertFalse(debouncer.should_emit(CAPTION, 1179))
        self.assertTrue(debouncer.should_emit(CAPTION, 1180))

    def test_debounces_each_signal_separately(self):
        debouncer = MediaCaptureHealthSignalDebouncer(180)

        self.assertTrue(debouncer.should_emit(CAPTION, 1000))
        self.assertTrue(debouncer.should_emit(AUDIO, 1001))


class CaptionsWithoutPerParticipantAudioCheckTest(SimpleTestCase):
    def record(self, check, *signals_and_timestamps):
        for signal, timestamp in signals_and_timestamps:
            check.record_signal(signal, timestamp)

    def test_finds_an_issue_after_three_caption_signals_without_audio(self):
        check = CaptionsWithoutPerParticipantAudioCheck()

        self.record(check, (CAPTION, 0), (CAPTION, 180))
        self.assertIsNone(check.evaluate())

        self.record(check, (CAPTION, 360))
        issue = check.evaluate()

        self.assertEqual(issue.issue_type, MediaCaptureIssueType.CAPTIONS_WITHOUT_PER_PARTICIPANT_AUDIO)
        self.assertEqual(issue.message, "Closed captions were generated over 6 minutes, but no per-participant audio was captured in that time. No per-participant audio has been captured in this meeting.")

    def test_per_participant_audio_resets_the_caption_signals(self):
        check = CaptionsWithoutPerParticipantAudioCheck()

        self.record(check, (CAPTION, 0), (CAPTION, 180), (AUDIO, 200), (CAPTION, 360), (CAPTION, 540))
        self.assertIsNone(check.evaluate())

    def test_says_when_per_participant_audio_stopped_partway_through(self):
        check = CaptionsWithoutPerParticipantAudioCheck()

        self.record(check, (AUDIO, 0), (CAPTION, 180), (CAPTION, 360), (CAPTION, 540))
        issue = check.evaluate()

        self.assertEqual(issue.message, "Closed captions were generated over 6 minutes, but no per-participant audio was captured in that time. Per-participant audio was captured earlier in the meeting, but then stopped.")

    def test_finds_no_issue_when_audio_and_captions_arrive_together(self):
        check = CaptionsWithoutPerParticipantAudioCheck()

        for minute in range(0, 60, 3):
            self.record(check, (AUDIO, minute * 60), (CAPTION, minute * 60 + 1))
            self.assertIsNone(check.evaluate())


@patch("bots.bot_controller.media_capture_health_monitor.time.time")
class MediaCaptureHealthMonitorTest(SimpleTestCase):
    def test_reports_each_issue_once(self, mock_time):
        on_issue_detected = MagicMock()
        monitor = MediaCaptureHealthMonitor(checks=[CaptionsWithoutPerParticipantAudioCheck()], on_issue_detected=on_issue_detected)

        for timestamp in (0, 180, 360, 540, 720, 900):
            mock_time.return_value = timestamp
            monitor.record_signal(CAPTION)
            monitor.evaluate()

        on_issue_detected.assert_called_once()
        issue = on_issue_detected.call_args.args[0]
        self.assertEqual(issue.issue_type, MediaCaptureIssueType.CAPTIONS_WITHOUT_PER_PARTICIPANT_AUDIO)
        self.assertIn("over 6 minutes", issue.message)

    def test_only_passes_a_check_the_signals_it_cares_about(self, mock_time):
        mock_time.return_value = 1
        check = MagicMock(relevant_signals=frozenset({CAPTION}))
        check.evaluate.return_value = None
        monitor = MediaCaptureHealthMonitor(checks=[check], on_issue_detected=MagicMock())

        monitor.record_signal(AUDIO)
        monitor.record_signal(CAPTION)

        check.record_signal.assert_called_once_with(CAPTION, 1)

    def test_an_error_reporting_an_issue_does_not_escape_and_is_not_retried(self, mock_time):
        mock_time.return_value = 0
        check = MagicMock(relevant_signals=frozenset())
        check.evaluate.return_value = MediaCaptureIssue(issue_type=MediaCaptureIssueType.CAPTIONS_WITHOUT_PER_PARTICIPANT_AUDIO, message="Something went wrong")
        on_issue_detected = MagicMock(side_effect=Exception("database is down"))
        monitor = MediaCaptureHealthMonitor(checks=[check], on_issue_detected=on_issue_detected)

        with self.assertLogs("bots.bot_controller.media_capture_health_monitor", level="ERROR"):
            monitor.evaluate()
        mock_time.return_value = 10
        monitor.evaluate()

        on_issue_detected.assert_called_once()
        self.assertEqual(monitor.checks, [])

    def test_a_check_that_raises_does_not_escape_and_is_dropped(self, mock_time):
        mock_time.return_value = 0
        failing_check = MagicMock(relevant_signals=frozenset())
        failing_check.evaluate.side_effect = Exception("bug in check")
        healthy_check = MagicMock(relevant_signals=frozenset())
        healthy_check.evaluate.return_value = None
        monitor = MediaCaptureHealthMonitor(checks=[failing_check, healthy_check], on_issue_detected=MagicMock())

        with self.assertLogs("bots.bot_controller.media_capture_health_monitor", level="ERROR"):
            monitor.evaluate()

        healthy_check.evaluate.assert_called_once()
        self.assertEqual(monitor.checks, [healthy_check])

    def test_runs_the_checks_at_most_every_five_seconds(self, mock_time):
        check = MagicMock(relevant_signals=frozenset())
        check.evaluate.return_value = None
        monitor = MediaCaptureHealthMonitor(checks=[check], on_issue_detected=MagicMock())

        for timestamp in (1000, 1000.1, 1004.9, 1005, 1009.9, 1010):
            mock_time.return_value = timestamp
            monitor.evaluate()

        self.assertEqual(check.evaluate.call_count, 3)


def json_message(payload):
    return (1).to_bytes(4, byteorder="little") + json.dumps(payload).encode("utf-8")


def per_participant_audio_message(participant_id="participant-1"):
    participant_id_bytes = participant_id.encode("utf-8")
    audio = np.zeros(480, dtype=np.float32).tobytes()
    return (5).to_bytes(4, byteorder="little") + len(participant_id_bytes).to_bytes(1, byteorder="little") + participant_id_bytes + audio


@patch("bots.web_bot_adapter.web_bot_adapter.time.time")
class WebBotAdapterMediaCaptureHealthSignalTest(SimpleTestCase):
    def setUp(self):
        self.adapter = object.__new__(WebBotAdapter)  # none of these tests need the adapter's constructor
        self.adapter.recording_paused = False
        self.adapter.add_audio_chunk_callback = MagicMock()
        self.adapter.media_capture_health_signal_callback = MagicMock()
        self.adapter.media_capture_health_signal_debouncer = MediaCaptureHealthSignalDebouncer(180)

    def receive_at(self, mock_time, now, message):
        mock_time.return_value = now
        self.adapter.handle_websocket([message])

    def test_caption_activity_is_sent_at_most_once_every_three_minutes(self, mock_time):
        self.receive_at(mock_time, 1000, json_message({"type": "CaptionActivity"}))
        self.receive_at(mock_time, 1060, json_message({"type": "CaptionActivity"}))
        self.receive_at(mock_time, 1180, json_message({"type": "CaptionActivity"}))

        self.assertEqual(self.adapter.media_capture_health_signal_callback.call_args_list, [((CAPTION,),), ((CAPTION,),)])

    def test_per_participant_audio_is_sent_at_most_once_every_three_minutes(self, mock_time):
        self.receive_at(mock_time, 1000, per_participant_audio_message())
        self.receive_at(mock_time, 1001, per_participant_audio_message())

        self.assertEqual(self.adapter.add_audio_chunk_callback.call_count, 2)
        self.assertEqual(self.adapter.media_capture_health_signal_callback.call_args_list, [((AUDIO,),)])

    def test_nothing_is_sent_while_recording_is_paused(self, mock_time):
        self.adapter.recording_paused = True

        self.receive_at(mock_time, 1000, json_message({"type": "CaptionActivity"}))
        self.receive_at(mock_time, 1000, per_participant_audio_message())

        self.adapter.media_capture_health_signal_callback.assert_not_called()

    def test_caption_activity_is_ignored_when_nothing_is_being_checked(self, mock_time):
        self.adapter.media_capture_health_signal_callback = None

        self.receive_at(mock_time, 1000, json_message({"type": "CaptionActivity"}))
        self.receive_at(mock_time, 1000, per_participant_audio_message())

        self.adapter.add_audio_chunk_callback.assert_called_once()


class BotControllerMediaCaptureHealthMonitorTest(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        project = Project.objects.create(name="Test Project", organization=organization)
        self.bot = Bot.objects.create(name="Test Bot", project=project, meeting_url="https://meet.google.com/abc-defg-hij")
        self.controller = object.__new__(BotController)  # the controller's constructor connects to the outside world
        self.controller.bot_in_db = self.bot
        self.controller.should_capture_audio_chunks = MagicMock(return_value=True)
        self.controller.per_participant_audio_input_manager = MagicMock()
        self.controller.pipeline_configuration = PipelineConfiguration.recorder_bot()

    def check_names(self):
        return [type(check).__name__ for check in self.controller.create_media_capture_health_monitor().checks]

    def test_audio_check_runs_when_per_participant_audio_chunks_are_captured(self):
        self.assertEqual(self.check_names(), ["CaptionsWithoutPerParticipantAudioCheck"])

    def test_audio_check_runs_when_per_participant_audio_is_only_streamed_over_websocket(self):
        self.controller.should_capture_audio_chunks.return_value = False
        self.controller.pipeline_configuration = PipelineConfiguration.recorder_bot(websocket_stream_per_participant_audio=True)

        self.assertEqual(self.check_names(), ["CaptionsWithoutPerParticipantAudioCheck"])

    def test_audio_check_runs_when_per_participant_audio_is_only_sent_to_room_sync(self):
        self.controller.should_capture_audio_chunks.return_value = False
        self.controller.pipeline_configuration = PipelineConfiguration.recorder_bot(room_sync_stream_per_participant_audio=True)

        self.assertEqual(self.check_names(), ["CaptionsWithoutPerParticipantAudioCheck"])

    def test_audio_check_does_not_run_when_nothing_uses_per_participant_audio(self):
        self.controller.should_capture_audio_chunks.return_value = False

        self.assertEqual(self.check_names(), [])

    @patch("bots.models.trigger_webhook")
    def test_issue_creates_an_error_log_entry_and_fires_the_webhook(self, mock_trigger_webhook):
        self.controller.on_media_capture_issue_detected(MediaCaptureIssue(issue_type=MediaCaptureIssueType.CAPTIONS_WITHOUT_PER_PARTICIPANT_AUDIO, message="Something went wrong"))

        log_entry = BotLogEntry.objects.get(bot=self.bot)
        self.assertEqual(log_entry.level, BotLogEntryLevels.ERROR)
        self.assertEqual(log_entry.entry_type, BotLogEntryTypes.PER_PARTICIPANT_AUDIO_CAPTURE_FAILURE)
        self.assertEqual(log_entry.message, "Something went wrong")
        self.assertEqual(mock_trigger_webhook.call_args.kwargs["payload"]["entry_type"], "per_participant_audio_capture_failure")
