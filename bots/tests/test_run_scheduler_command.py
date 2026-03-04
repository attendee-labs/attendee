import base64
import json
import signal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone as django_timezone

from accounts.models import Organization
from bots.management.commands.run_scheduler import CALENDAR_SYNC_THRESHOLD_HOURS, Command
from bots.models import Bot, BotStates, Calendar, CalendarPlatform, CalendarStates, Project, ZoomOAuthApp, ZoomOAuthConnection, ZoomOAuthConnectionStates


def _build_celery_unacked_entry(bot_id, join_at_iso):
    """Build a mock Redis unacked hash entry matching the Celery message format."""
    body = json.dumps([[bot_id, join_at_iso]])
    encoded_body = base64.b64encode(body.encode()).decode()
    message = [{"body": encoded_body, "headers": {"task": "bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot"}}]
    return json.dumps(message).encode()


class RunSchedulerCommandTestCase(TestCase):
    def setUp(self):
        """Set up test data"""
        self.organization = Organization.objects.create(
            name="Test Organization",
            centicredits=10000,  # 100 credits
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create test times
        self.now = django_timezone.now().replace(microsecond=0, second=0)
        self.join_at_within_threshold = self.now + django_timezone.timedelta(minutes=3)
        self.join_at_too_early = self.now + django_timezone.timedelta(minutes=7)  # Outside threshold
        self.join_at_too_late = self.now - django_timezone.timedelta(minutes=7)  # Outside threshold

    def test_run_scheduled_bots_launches_eligible_bots(self):
        """Test that _run_scheduled_bots finds and launches bots within the time threshold"""
        # Create bots with different states and times
        eligible_bot = Bot.objects.create(project=self.project, name="Eligible Bot", meeting_url="https://example.zoom.us/j/123456789", state=BotStates.SCHEDULED, join_at=self.join_at_within_threshold)

        # Bot that's too early (outside threshold)
        Bot.objects.create(project=self.project, name="Too Early Bot", meeting_url="https://example.zoom.us/j/987654321", state=BotStates.SCHEDULED, join_at=self.join_at_too_early)

        # Bot that's not in SCHEDULED state
        Bot.objects.create(project=self.project, name="Wrong State Bot", meeting_url="https://example.zoom.us/j/111222333", state=BotStates.READY, join_at=self.join_at_within_threshold)

        command = Command()

        with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_scheduled_bots()

            # Verify only the eligible bot was launched
            mock_delay.assert_called_once_with(eligible_bot.id, self.join_at_within_threshold.isoformat())

    def test_graceful_shutdown_signal_handling(self):
        """Test that the signal handler properly sets the shutdown flag"""
        command = Command()

        # Verify initial state
        self.assertTrue(command._keep_running)

        # Simulate receiving SIGTERM
        command._graceful_exit(signal.SIGTERM, None)

        # Verify the shutdown flag was set
        self.assertFalse(command._keep_running)

    def test_run_scheduled_bots_ignores_bots_outside_time_threshold(self):
        """Test that bots outside the 5-minute time window are ignored"""
        # Create a bot that's too late (missed by more than 5 minutes)
        Bot.objects.create(project=self.project, name="Too Late Bot", meeting_url="https://example.zoom.us/j/444555666", state=BotStates.SCHEDULED, join_at=self.join_at_too_late)

        # Create a bot that's too early (more than 5 minutes in the future)
        Bot.objects.create(project=self.project, name="Too Early Bot", meeting_url="https://example.zoom.us/j/777888999", state=BotStates.SCHEDULED, join_at=self.join_at_too_early)

        command = Command()

        with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_scheduled_bots()

            # Verify no bots were launched since they're all outside the time threshold
            mock_delay.assert_not_called()

    def test_run_periodic_calendar_syncs_with_no_eligible_calendars(self):
        """Test that _run_periodic_calendar_syncs handles the case when no calendars need syncing"""
        # Create a calendar that was synced recently
        recent_sync_time = self.now - django_timezone.timedelta(hours=12)
        Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, state=CalendarStates.CONNECTED, sync_task_enqueued_at=recent_sync_time, client_id="test_client_id")

        command = Command()

        with patch("bots.tasks.sync_calendar_task.enqueue_sync_calendar_task") as mock_enqueue:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_periodic_calendar_syncs()

            # Verify no sync tasks were enqueued
            mock_enqueue.assert_not_called()

    def test_run_periodic_calendar_syncs_handles_boundary_conditions(self):
        """Test calendar sync with calendars exactly at the threshold boundary"""
        # Calendar synced exactly at threshold (should be included)
        exactly_at_threshold = self.now - django_timezone.timedelta(hours=CALENDAR_SYNC_THRESHOLD_HOURS)
        calendar_boundary = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, state=CalendarStates.CONNECTED, sync_task_enqueued_at=exactly_at_threshold, client_id="test_client_id_boundary")

        # Calendar synced just under threshold (should be excluded)
        just_under_threshold = self.now - django_timezone.timedelta(hours=CALENDAR_SYNC_THRESHOLD_HOURS, minutes=-1)
        calendar_just_under = Calendar.objects.create(project=self.project, platform=CalendarPlatform.MICROSOFT, state=CalendarStates.CONNECTED, sync_task_enqueued_at=just_under_threshold, client_id="test_client_id_under")

        command = Command()

        with patch("bots.tasks.sync_calendar_task.sync_calendar.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_periodic_calendar_syncs()

            # Verify only the boundary calendar had a sync task enqueued
            mock_delay.assert_called_once_with(calendar_boundary.id)

        # Verify the sync_task_enqueued_at field was updated for the boundary calendar
        calendar_boundary.refresh_from_db()
        calendar_just_under.refresh_from_db()
        self.assertEqual(calendar_boundary.sync_task_enqueued_at, self.now)
        self.assertEqual(calendar_just_under.sync_task_enqueued_at, just_under_threshold)
        self.assertEqual(calendar_just_under.sync_task_requested_at, None)

    def test_run_periodic_calendar_syncs_handles_requested_syncs(self):
        """Test calendar sync with calendars that have a requested sync"""
        # Calendar synced recently, but with a requested sync (should be included)
        exactly_five_minutes_ago = self.now - django_timezone.timedelta(minutes=5)
        exactly_24_hours_ago = self.now - django_timezone.timedelta(hours=24)
        calendar_with_requested_sync = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, state=CalendarStates.CONNECTED, sync_task_enqueued_at=exactly_five_minutes_ago, sync_task_requested_at=exactly_24_hours_ago, client_id="test_client_id_boundary")

        command = Command()

        with patch("bots.tasks.sync_calendar_task.sync_calendar.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_periodic_calendar_syncs()

            # Verify only the boundary calendar had a sync task enqueued
            mock_delay.assert_called_once_with(calendar_with_requested_sync.id)

        # Verify the sync_task_enqueued_at field was updated for the boundary calendar
        calendar_with_requested_sync.refresh_from_db()
        self.assertEqual(calendar_with_requested_sync.sync_task_enqueued_at, self.now)
        self.assertEqual(calendar_with_requested_sync.sync_task_requested_at, None)

    def test_run_autopay_tasks_enqueues_eligible_organizations(self):
        """Test that _run_autopay_tasks finds and enqueues autopay tasks for eligible organizations"""
        # Create organization eligible for autopay
        eligible_org = Organization.objects.create(
            name="Eligible Autopay Org",
            centicredits=500,  # 5 credits, below default threshold of 10
            autopay_enabled=True,
            autopay_threshold_centricredits=1000,  # 10 credits threshold
            autopay_amount_to_purchase_cents=2000,  # $20
            autopay_stripe_customer_id="cus_test123",
            autopay_charge_failure_data=None,
        )

        # Create organization not eligible (autopay disabled)
        Organization.objects.create(
            name="Autopay Disabled Org",
            centicredits=500,
            autopay_enabled=False,
            autopay_threshold_centricredits=1000,
            autopay_stripe_customer_id="cus_test456",
        )

        # Create organization not eligible (above threshold)
        Organization.objects.create(
            name="Above Threshold Org",
            centicredits=1500,  # 15 credits, above threshold
            autopay_enabled=True,
            autopay_threshold_centricredits=1000,
            autopay_stripe_customer_id="cus_test789",
        )

        command = Command()

        with patch("bots.tasks.autopay_charge_task.autopay_charge.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_autopay_tasks()

            # Verify only the eligible organization had an autopay task enqueued
            mock_delay.assert_called_once_with(eligible_org.id)

    def test_run_autopay_tasks_excludes_organizations_with_recent_charge_tasks(self):
        """Test that _run_autopay_tasks excludes organizations that had charge tasks enqueued recently"""
        # Create organization that would be eligible but had a charge task enqueued recently
        recent_charge_time = self.now - django_timezone.timedelta(hours=12)  # 12 hours ago, within 24 hour window
        Organization.objects.create(
            name="Recent Charge Org",
            centicredits=500,  # Below threshold
            autopay_enabled=True,
            autopay_threshold_centricredits=1000,
            autopay_stripe_customer_id="cus_recent123",
            autopay_charge_task_enqueued_at=recent_charge_time,
        )

        # Create organization eligible (charge task enqueued more than 24 hours ago)
        old_charge_time = self.now - django_timezone.timedelta(days=2)  # 2 days ago, outside 24 hour window
        old_charge_org = Organization.objects.create(
            name="Old Charge Org",
            centicredits=500,  # Below threshold
            autopay_enabled=True,
            autopay_threshold_centricredits=1000,
            autopay_stripe_customer_id="cus_old456",
            autopay_charge_task_enqueued_at=old_charge_time,
        )

        command = Command()

        with patch("bots.tasks.autopay_charge_task.autopay_charge.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_autopay_tasks()

            # Verify only the organization with old charge task had an autopay task enqueued
            mock_delay.assert_called_once_with(old_charge_org.id)

    def test_run_periodic_zoom_oauth_connection_syncs_handles_boundary_conditions(self):
        """Test zoom oauth connection sync with connections exactly at the 7 day boundary"""
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")

        # Connection synced exactly 7 days ago (should be included)
        exactly_7_days_ago = self.now - django_timezone.timedelta(days=7)
        connection_boundary = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="user_boundary",
            account_id="account_boundary",
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_local_recording_token_supported=True,
            sync_task_enqueued_at=exactly_7_days_ago,
        )

        # Connection synced just under 7 days ago (should be excluded)
        just_under_7_days_ago = self.now - django_timezone.timedelta(days=6, hours=23)
        connection_just_under = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="user_under",
            account_id="account_under",
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_local_recording_token_supported=True,
            sync_task_enqueued_at=just_under_7_days_ago,
        )

        command = Command()

        with patch("bots.tasks.sync_zoom_oauth_connection_task.sync_zoom_oauth_connection.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_periodic_zoom_oauth_connection_syncs()

            # Verify only the boundary connection had a sync task enqueued
            mock_delay.assert_called_once_with(connection_boundary.id)

        # Verify the sync_task_enqueued_at field was updated for the boundary connection
        connection_boundary.refresh_from_db()
        connection_just_under.refresh_from_db()
        self.assertEqual(connection_boundary.sync_task_enqueued_at, self.now)
        self.assertEqual(connection_just_under.sync_task_enqueued_at, just_under_7_days_ago)

    def test_run_periodic_zoom_oauth_connection_token_refreshs_handles_boundary_conditions(self):
        """Test zoom oauth connection token refresh with connections exactly at the 30 day boundary"""
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")

        # Connection refreshed exactly 30 days ago (should be included)
        exactly_30_days_ago = self.now - django_timezone.timedelta(days=30)
        connection_boundary = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="user_boundary",
            account_id="account_boundary",
            state=ZoomOAuthConnectionStates.CONNECTED,
            token_refresh_task_enqueued_at=exactly_30_days_ago,
        )

        # Connection refreshed just under 30 days ago (should be excluded)
        just_under_30_days_ago = self.now - django_timezone.timedelta(days=29, hours=23)
        connection_just_under = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="user_under",
            account_id="account_under",
            state=ZoomOAuthConnectionStates.CONNECTED,
            token_refresh_task_enqueued_at=just_under_30_days_ago,
        )

        command = Command()

        with patch("bots.tasks.refresh_zoom_oauth_connection_task.refresh_zoom_oauth_connection.delay") as mock_delay:
            with patch("django.utils.timezone.now", return_value=self.now):
                command._run_periodic_zoom_oauth_connection_token_refreshs()

            # Verify only the boundary connection had a refresh task enqueued
            mock_delay.assert_called_once_with(connection_boundary.id)

        # Verify the token_refresh_task_enqueued_at field was updated for the boundary connection
        connection_boundary.refresh_from_db()
        connection_just_under.refresh_from_db()
        self.assertEqual(connection_boundary.token_refresh_task_enqueued_at, self.now)
        self.assertEqual(connection_just_under.token_refresh_task_enqueued_at, just_under_30_days_ago)

    def test_run_scheduled_bots_with_jitter_launches_immediately_below_threshold(self):
        """Test that bots with join_at below the jitter start threshold are launched immediately via .delay()"""
        jitter_start = 300
        jitter_end = 600

        # Bot within [now - 5min, now + jitter_start] should launch immediately
        bot = Bot.objects.create(
            project=self.project,
            name="Immediate Bot",
            meeting_url="https://example.zoom.us/j/123456789",
            state=BotStates.SCHEDULED,
            join_at=self.now + django_timezone.timedelta(seconds=jitter_start - 60),
        )

        command = Command()
        mock_redis = MagicMock()
        mock_redis.hscan_iter.return_value = iter([])
        command._redis_client = mock_redis

        with patch.dict("os.environ", {"SCHEDULED_BOT_JITTER_START_SECONDS": str(jitter_start), "SCHEDULED_BOT_JITTER_END_SECONDS": str(jitter_end)}):
            with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.delay") as mock_delay:
                with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.apply_async") as mock_apply_async:
                    with patch("django.utils.timezone.now", return_value=self.now):
                        command._run_scheduled_bots_with_jitter()

                    mock_delay.assert_called_once_with(bot.id, bot.join_at.isoformat())
                    mock_apply_async.assert_not_called()

    def test_run_scheduled_bots_with_jitter_launches_with_delay_above_threshold(self):
        """Test that bots with join_at above the jitter start threshold are launched with apply_async and a countdown"""
        jitter_start = 300
        jitter_end = 600

        # Bot within (now + jitter_start, now + jitter_end] should launch with random delay
        bot_join_at = self.now + django_timezone.timedelta(seconds=jitter_start + 120)
        bot = Bot.objects.create(
            project=self.project,
            name="Jittered Bot",
            meeting_url="https://example.zoom.us/j/123456789",
            state=BotStates.SCHEDULED,
            join_at=bot_join_at,
        )

        command = Command()
        mock_redis = MagicMock()
        mock_redis.hscan_iter.return_value = iter([])
        command._redis_client = mock_redis

        with patch.dict("os.environ", {"SCHEDULED_BOT_JITTER_START_SECONDS": str(jitter_start), "SCHEDULED_BOT_JITTER_END_SECONDS": str(jitter_end)}):
            with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.delay") as mock_delay:
                with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.apply_async") as mock_apply_async:
                    with patch("django.utils.timezone.now", return_value=self.now):
                        with patch("random.randint", return_value=42) as mock_randint:
                            command._run_scheduled_bots_with_jitter()

                    mock_delay.assert_not_called()
                    mock_apply_async.assert_called_once_with(args=[bot.id, bot_join_at.isoformat()], countdown=42)
                    # The max delay should be (bot.join_at - jitter_threshold).total_seconds() = 120 seconds
                    mock_randint.assert_called_once_with(0, 120)

    def test_run_scheduled_bots_with_jitter_skips_already_pending_bots(self):
        """Test that bots already in pending launch tasks are skipped"""
        jitter_start = 300
        jitter_end = 600

        bot = Bot.objects.create(
            project=self.project,
            name="Already Pending Bot",
            meeting_url="https://example.zoom.us/j/123456789",
            state=BotStates.SCHEDULED,
            join_at=self.now + django_timezone.timedelta(seconds=60),
        )

        command = Command()
        mock_redis = MagicMock()
        mock_redis.hscan_iter.return_value = iter(
            [
                (b"delivery-tag-1", _build_celery_unacked_entry(bot.id, bot.join_at.isoformat())),
            ]
        )
        command._redis_client = mock_redis

        with patch.dict("os.environ", {"SCHEDULED_BOT_JITTER_START_SECONDS": str(jitter_start), "SCHEDULED_BOT_JITTER_END_SECONDS": str(jitter_end)}):
            with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.delay") as mock_delay:
                with patch("bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot.apply_async") as mock_apply_async:
                    with patch("django.utils.timezone.now", return_value=self.now):
                        command._run_scheduled_bots_with_jitter()

                    mock_delay.assert_not_called()
                    mock_apply_async.assert_not_called()
