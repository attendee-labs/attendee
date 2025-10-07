import copy
import logging
import re
from datetime import datetime, timedelta
from datetime import timezone as python_timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import dateutil.parser
import requests
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from bots.bots_api_utils import delete_bot, patch_bot
from bots.calendars_api_utils import remove_bots_from_calendar
from bots.meeting_url_utils import meeting_type_from_url
from bots.models import Bot, BotStates, Calendar, CalendarEvent, CalendarPlatform, CalendarStates, WebhookTriggerTypes
from bots.webhook_payloads import calendar_webhook_payload
from bots.webhook_utils import trigger_webhook

logger = logging.getLogger(__name__)

URL_CANDIDATE = re.compile(r"https?://[^\s<>\"']+")


def extract_meeting_url_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    for m in URL_CANDIDATE.finditer(text):
        url = m.group(0).rstrip(").,;]}>")
        if meeting_type_from_url(url):
            return url
    return None


def _exception_is_404(e: Exception) -> bool:
    """Check if an exception is a 404."""
    # Check if it's a requests HTTPError with status code 404
    if isinstance(e, requests.HTTPError) and hasattr(e, "response") and e.response is not None:
        return e.response.status_code == 404

    return False


def sync_bot_with_calendar_event(bot: Bot, calendar_event: CalendarEvent):
    """Sync a bot with a calendar event."""
    # If the calendar event is deleted, delete the bot
    if calendar_event.is_deleted:
        logger.info(f"Calendar event {calendar_event.platform_uuid} is deleted, deleting bot {bot.object_id}")
        success, error = delete_bot(bot)
        if error:
            logger.error(f"Failed to delete bot {bot.object_id}: {error}")
        else:
            logger.info(f"Successfully deleted bot {bot.object_id}")
        return

    # Check if bot needs to be updated to match calendar event
    update_data = {}

    # Check meeting_url
    if bot.meeting_url != calendar_event.meeting_url:
        logger.info(f"Bot {bot.object_id} meeting_url differs from calendar event: {bot.meeting_url} -> {calendar_event.meeting_url}")
        update_data["meeting_url"] = calendar_event.meeting_url

    # Check join_at (bot.join_at should match calendar_event.start_time)
    if bot.join_at != calendar_event.start_time:
        logger.info(f"Bot {bot.object_id} join_at differs from calendar event start_time: {bot.join_at} -> {calendar_event.start_time}")
        update_data["join_at"] = calendar_event.start_time

    # If updates are needed, patch the bot
    if update_data:
        logger.info(f"Patching bot {bot.object_id} to sync with calendar event {calendar_event.platform_uuid} with data {update_data}")
        updated_bot, error = patch_bot(bot, update_data)
        if error:
            logger.error(f"Failed to patch bot {bot.object_id}: {error}")
        else:
            logger.info(f"Successfully patched bot {bot.object_id}")


def sync_bots_for_calendar_event(calendar_event: CalendarEvent):
    """Sync the scheduled bots of a calendar event. Bots for the event that are not in the scheduled state cannot be changed so will be ignored."""
    for bot in calendar_event.bots.filter(state=BotStates.SCHEDULED):
        sync_bot_with_calendar_event(bot, calendar_event)


def enqueue_sync_calendar_task(calendar: Calendar):
    """Enqueue a sync calendar task for a calendar."""
    with transaction.atomic():
        calendar.sync_task_enqueued_at = timezone.now()
        calendar.sync_task_requested_at = None
        calendar.save()
        sync_calendar.delay(calendar.id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def sync_calendar(self, calendar_id):
    """Celery task to sync calendar events with a remote calendar."""
    logger.info(f"Syncing calendar {calendar_id}")
    calendar = Calendar.objects.get(id=calendar_id)
    if calendar.platform == CalendarPlatform.GOOGLE:
        sync_handler = GoogleCalendarSyncHandler(calendar_id)
    elif calendar.platform == CalendarPlatform.MICROSOFT:
        sync_handler = MicrosoftCalendarSyncHandler(calendar_id)
    else:
        raise ValueError(f"Unsupported calendar platform: {calendar.platform}")
    return sync_handler.sync_events()


class CalendarAPIError(Exception):
    """Custom exception for Remote Calendar API errors."""

    pass


class CalendarAPIAuthenticationError(CalendarAPIError):
    """Custom exception for Google Calendar API errors."""

    pass


class CalendarSyncHandler:
    """Handler for syncing calendar events with a remote calendar."""

    def __init__(self, calendar_id: int):
        self.calendar = Calendar.objects.get(id=calendar_id)
        self.time_window_start: Optional[datetime] = None
        self.time_window_end: Optional[datetime] = None

    def _get_local_events_in_window(self) -> Dict[str, CalendarEvent]:
        """Get all local calendar events within the time window."""
        local_events = CalendarEvent.objects.filter(calendar=self.calendar, start_time__gte=self.time_window_start, start_time__lt=self.time_window_end, is_deleted=False)

        # Return dict keyed by platform_uuid for easy lookup
        return {event.platform_uuid: event for event in local_events}

    def _upsert_calendar_event(self, remote_event: dict) -> tuple[CalendarEvent, bool, bool]:
        """
        Upsert a calendar event from remote calendar data.

        Returns:
            tuple: (CalendarEvent instance, was_created, was_updated)
        """
        event_data = self._remote_event_to_calendar_event_data(remote_event)
        platform_uuid = event_data["platform_uuid"]

        try:
            # Try to get existing event
            local_event = CalendarEvent.objects.get(calendar=self.calendar, platform_uuid=platform_uuid)

            # Check if raw data has changed or meeting url has changed due to changed extraction logic
            if local_event.raw == event_data["raw"] and local_event.meeting_url == event_data["meeting_url"]:
                return local_event, False, False

            # Update the existing event
            for field, value in event_data.items():
                setattr(local_event, field, value)
            local_event.save()

            # Sync the bots for the calendar event
            sync_bots_for_calendar_event(local_event)

            return local_event, False, True

        except CalendarEvent.DoesNotExist:
            # Create new event
            local_event = CalendarEvent.objects.create(calendar=self.calendar, **event_data)
            return local_event, True, False

    def _mark_calendar_event_as_deleted(self, local_event: CalendarEvent):
        """Mark an event as deleted in the local database."""
        local_event.is_deleted = True
        local_event.save()

        # Sync the bots for the calendar event
        sync_bots_for_calendar_event(local_event)

    def sync_events(self) -> dict:
        """
        Main sync method that coordinates the entire sync process.

        Returns:
            dict: Summary of sync results
        """
        try:
            # Step 0: Set time window
            now = timezone.now()
            self.time_window_start = now - timedelta(days=1)
            self.time_window_end = now + timedelta(days=28)
            logger.info(f"Set time window for calendar {self.calendar.object_id}: {self.time_window_start.isoformat()} to {self.time_window_end.isoformat()}")

            # Get access token
            access_token = self._get_access_token()

            # Set the sync start time
            sync_started_at = timezone.now()

            # Step 1: Pull from Remote Calendar

            # Step 1a: List all events from Remote Calendar within time window
            remote_events = self._list_events(access_token)
            remote_event_ids = {event["id"] for event in remote_events}

            # Start transaction
            with transaction.atomic():
                # Step 1b: Find local events not in the remote fetch and get them individually
                local_events = self._get_local_events_in_window()
                local_events_missing_from_remote = set(local_events.keys()) - remote_event_ids

                checked_individually_count = 0
                deleted_count = 0
                for missing_event_id in local_events_missing_from_remote:
                    try:
                        individual_remote_event = self._get_event_by_id(missing_event_id, access_token)
                        checked_individually_count += 1

                        if individual_remote_event:
                            remote_events.append(individual_remote_event)
                        else:
                            # Event was deleted from Remote Calendar, mark as deleted
                            self._mark_calendar_event_as_deleted(local_events[missing_event_id])
                            logger.info(f"Marked event {missing_event_id} as deleted")
                            deleted_count += 1
                    except Exception as e:
                        logger.error(f"Failed to check individual event {missing_event_id}: {e}")

                # Step 2: Diff against local DB - upsert all Remote events
                created_count = 0
                updated_count = 0

                for remote_event in remote_events:
                    remote_event_id = remote_event["id"]
                    local_event, was_created, was_updated = self._upsert_calendar_event(remote_event)

                    if was_created:
                        created_count += 1
                        logger.info(f"Created event {remote_event_id}")
                    elif was_updated:
                        updated_count += 1
                        logger.info(f"Updated event {remote_event_id}")

                # Update calendar sync success timestamp and window
                self.calendar.last_attempted_sync_at = timezone.now()
                self.calendar.last_successful_sync_at = self.calendar.last_attempted_sync_at
                self.calendar.last_successful_sync_time_window_start = self.time_window_start
                self.calendar.last_successful_sync_time_window_end = self.time_window_end
                self.calendar.last_successful_sync_started_at = sync_started_at
                self.calendar.state = CalendarStates.CONNECTED
                self.calendar.connection_failure_data = None
                self.calendar.save()

                sync_results = {
                    "success": True,
                    "created_count": created_count,
                    "updated_count": updated_count,
                    "deleted_count": deleted_count,
                    "checked_individually_count": checked_individually_count,
                    "total_remote_events": len(remote_events),
                    "total_local_events": len(local_events),
                    "time_window_start": self.time_window_start.isoformat(),
                    "time_window_end": self.time_window_end.isoformat(),
                }

                logger.info(f"Calendar sync completed successfully: {sync_results}")

                trigger_webhook(
                    webhook_trigger_type=WebhookTriggerTypes.CALENDAR_EVENTS_UPDATE,
                    calendar=self.calendar,
                    payload=calendar_webhook_payload(self.calendar),
                )

                return sync_results

        except CalendarAPIAuthenticationError as e:
            # Update calendar state to indicate failure
            with transaction.atomic():
                remove_bots_from_calendar(calendar=self.calendar, project=self.calendar.project)
                self.calendar.state = CalendarStates.DISCONNECTED
                self.calendar.connection_failure_data = {
                    "error": str(e),
                    "timestamp": timezone.now().isoformat(),
                }
                self.calendar.save()

            logger.exception(f"Calendar sync failed with CalendarAPIAuthenticationError for {self.calendar.object_id}: {e}")

            # Create webhook event
            trigger_webhook(
                webhook_trigger_type=WebhookTriggerTypes.CALENDAR_STATE_CHANGE,
                calendar=self.calendar,
                payload=calendar_webhook_payload(self.calendar),
            )

        except Exception as e:
            logger.exception(f"Calendar sync failed with {type(e).__name__} for {self.calendar.object_id}: {e}")
            self.calendar.last_attempted_sync_at = timezone.now()
            self.calendar.save()
            raise


class GoogleCalendarSyncHandler(CalendarSyncHandler):
    """Handler for syncing calendar events with Google Calendar API."""

    def _raise_if_error_is_authentication_error(self, e: requests.RequestException):
        error_code = e.response.json().get("error")
        if error_code == "invalid_grant" or error_code == "invalid_client":
            raise CalendarAPIAuthenticationError(f"Google Authentication error: {e.response.json()}")
        if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in e.response.text:
            raise CalendarAPIAuthenticationError(f"Google Authentication error: {e.response.json()}")
        if "PERMISSION_DENIED" in e.response.text:
            raise CalendarAPIAuthenticationError(f"Google Authentication error: {e.response.json()}")

        return

    def _get_access_token(self) -> str:
        """Get a fresh access token using the refresh token."""
        credentials = self.calendar.get_credentials()
        if not credentials:
            raise CalendarAPIAuthenticationError("No credentials found for calendar")

        refresh_token = credentials.get("refresh_token")
        client_secret = credentials.get("client_secret")

        if not refresh_token or not client_secret:
            raise CalendarAPIAuthenticationError("Missing refresh_token or client_secret")

        # Exchange refresh token for access token
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.calendar.client_id,
            "client_secret": client_secret,
        }

        try:
            response = requests.post("https://oauth2.googleapis.com/token", data=data, timeout=30)
            response.raise_for_status()
            token_data = response.json()

            if "access_token" not in token_data:
                raise CalendarAPIError(f"No access_token in response. Response body: {response.json()}")

            return token_data["access_token"]

        except requests.RequestException as e:
            self._raise_if_error_is_authentication_error(e)
            raise CalendarAPIError(f"Failed to refresh Google access token. Response body: {e.response.json()}")

    def _make_gcal_request(self, url: str, access_token: str, params: dict = None) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"}
        # Optional: log the fully encoded URL
        req = requests.Request("GET", url, headers=headers, params=params).prepare()
        logger.info("Fetching Google Calendar events: %s", req.url)

        try:
            # Send the request
            with requests.Session() as s:
                resp = s.send(req, timeout=25)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            self._raise_if_error_is_authentication_error(e)
            logger.exception(f"Failed to make Google Calendar request. Response body: {e.response.json()}")
            raise e

    def _list_events(self, access_token: str) -> List[dict]:
        """List all events from Google Calendar within the time window."""
        calendar_id = self.calendar.platform_uuid or "primary"

        # Format times for Google Calendar API (RFC3339)
        time_min = self.time_window_start.isoformat()
        time_max = self.time_window_end.isoformat()

        base_url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
        base_params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",  # Expand recurring events
            "showDeleted": "true",
            "maxResults": 2500,  # Google's max
        }

        all_events = []
        next_page_token = None

        while True:
            params = dict(base_params)  # copy base params
            if next_page_token:
                params["pageToken"] = next_page_token

            logger.info(f"Fetching Google Calendar events: {base_url} with params: {params}")
            response_data = self._make_gcal_request(base_url, access_token, params)

            events = response_data.get("items", [])
            all_events.extend(events)

            next_page_token = response_data.get("nextPageToken")
            if not next_page_token:
                break

        logger.info(f"Fetched {len(all_events)} events from Google Calendar")
        return all_events

    def _get_event_by_id(self, event_id: str, access_token: str) -> Optional[dict]:
        """Get a specific event by ID from Google Calendar."""
        calendar_id = self.calendar.platform_uuid or "primary"
        url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"

        try:
            logger.info(f"Fetching individual event {event_id} from Google Calendar")
            return self._make_gcal_request(url, access_token)
        except Exception as e:
            if _exception_is_404(e):
                logger.info(f"Event {event_id} not found in Google Calendar")
                # Event was deleted
                return None
            raise

    def _parse_event_datetime(self, event_datetime: dict, default_tz: str = "UTC", normalize_to_utc: bool = False) -> datetime:
        """Parse Google Calendar event datetime dict (start/end)."""
        if not event_datetime:
            raise ValueError("Invalid event datetime")

        tzname = event_datetime.get("timeZone") or default_tz

        if "dateTime" in event_datetime:
            dt = dateutil.parser.isoparse(event_datetime["dateTime"])  # handles Z, offsets, subsecond
            if dt.tzinfo is None:  # just in case a "floating" time arrives
                dt = dt.replace(tzinfo=ZoneInfo(tzname))
        elif "date" in event_datetime:
            # All-day: interpret as midnight at the event's timezone
            d = datetime.strptime(event_datetime["date"], "%Y-%m-%d").date()
            dt = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tzname))
        else:
            raise ValueError(f"Invalid event datetime format: {event_datetime}")

        return dt.astimezone(python_timezone.utc) if normalize_to_utc else dt

    def _truncate_large_text_fields_in_gcal_event(self, google_event: dict) -> dict:
        """Truncate large text fields in a Google Calendar event. Return a copy of the event with the fields truncated."""
        event_copy = google_event.copy()
        if event_copy.get("description"):
            event_copy["description"] = event_copy.get("description")[:8000]
        if event_copy.get("summary"):
            event_copy["summary"] = event_copy.get("summary")[:8000]
        return event_copy

    def _remote_event_to_calendar_event_data(self, google_event: dict) -> dict:
        """Convert Google Calendar event data to CalendarEvent field data."""
        start_time = self._parse_event_datetime(google_event["start"])
        end_time = self._parse_event_datetime(google_event["end"])

        # Extract meeting URL if present
        meeting_url_from_conference_data = None
        if "conferenceData" in google_event:
            entry_points = google_event["conferenceData"].get("entryPoints", [])
            for entry_point in entry_points:
                if entry_point.get("entryPointType") == "video":
                    meeting_url_from_conference_data = entry_point.get("uri")
                    break
        meeting_url = extract_meeting_url_from_text(meeting_url_from_conference_data) or extract_meeting_url_from_text(google_event.get("hangoutLink")) or extract_meeting_url_from_text(google_event.get("location")) or extract_meeting_url_from_text(google_event.get("description")) or extract_meeting_url_from_text(google_event.get("summary"))

        # Extract attendees
        attendees = []
        if "attendees" in google_event:
            for attendee in google_event["attendees"]:
                attendees.append(
                    {
                        "email": attendee.get("email"),
                        "name": attendee.get("displayName"),
                    }
                )

        return {
            "platform_uuid": google_event["id"],
            "meeting_url": meeting_url,
            "name": google_event.get("summary"),
            "start_time": start_time,
            "end_time": end_time,
            "attendees": attendees,
            "raw": self._truncate_large_text_fields_in_gcal_event(google_event),
            "is_deleted": google_event.get("status") == "cancelled",
            "ical_uid": google_event.get("iCalUID"),
        }


class MicrosoftCalendarSyncHandler(CalendarSyncHandler):
    """
    Handler for syncing calendar events with Microsoft Graph Calendar API.

    Notes:
    - We use /me/calendarView to get expanded instances within a time window.
    - We set Prefer: outlook.timezone="UTC" so all dateTimes are returned in UTC.
    - Microsoft rotates the refresh_token on each refresh. We update the stored
      credentials with the new refresh_token when present.
    """

    TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    CALENDAR_EVENT_SELECT_FIELDS = "id,subject,start,end,attendees,organizer,iCalUId,seriesMasterId,isCancelled,isOnlineMeeting,onlineMeetingProvider,onlineMeeting,onlineMeetingUrl,location,body,webLink"

    def _raise_if_error_is_authentication_error(self, e: requests.RequestException):
        if e.response.json().get("error") == "invalid_grant":
            raise CalendarAPIAuthenticationError(f"Microsoft Authentication error: {e.response.json()}")

        if "ErrorAccessDenied" in e.response.text:
            raise CalendarAPIAuthenticationError(f"Microsoft Authentication error: {e.response.json()}")
        return

    # ---------------------------
    # Auth
    # ---------------------------
    def _get_access_token(self) -> str:
        """
        Exchange the stored refresh token for a new access token.
        Microsoft returns a new refresh_token on each successful refresh.
        Persist it so we don't lose the chain.
        """
        credentials = self.calendar.get_credentials()
        if not credentials:
            raise CalendarAPIAuthenticationError("No credentials found for calendar")

        refresh_token = credentials.get("refresh_token")
        client_secret = credentials.get("client_secret")
        if not refresh_token or not client_secret:
            raise CalendarAPIAuthenticationError("Missing refresh_token or client_secret")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.calendar.client_id,
            "client_secret": client_secret,
        }

        try:
            response = requests.post(self.TOKEN_URL, data=data, timeout=30)
            response.raise_for_status()
            token_data = response.json()

            access_token = token_data.get("access_token")
            if not access_token:
                raise CalendarAPIError(f"No access_token in refresh response. Response body: {response.json()}")

            # IMPORTANT: Microsoft rotates refresh tokens. Save the new one if provided.
            new_refresh = token_data.get("refresh_token")
            if new_refresh and new_refresh != refresh_token:
                credentials["refresh_token"] = new_refresh
                self.calendar.set_credentials(credentials)
                logger.info("Stored rotated Microsoft refresh_token for calendar %s", self.calendar.object_id)

            return access_token

        except requests.RequestException as e:
            self._raise_if_error_is_authentication_error(e)
            raise CalendarAPIError(f"Failed to refresh Microsoft access token. Response body: {e.response.json()}")

    # ---------------------------
    # HTTP helpers
    # ---------------------------
    def _make_graph_request(self, url: str, access_token: str, params: dict | None = None) -> dict:
        """
        Make a Microsoft Graph request with proper headers. If url is a full @odata.nextLink,
        we pass it as-is and ignore params.
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            # Ensure dateTimes are emitted in UTC so we can parse deterministically.
            "Prefer": 'outlook.timezone="UTC"',
        }

        # Build request
        if params is None:
            req = requests.Request("GET", url, headers=headers).prepare()
        else:
            req = requests.Request("GET", url, headers=headers, params=params).prepare()

        logger.info("Fetching Microsoft Graph: %s", req.url)

        try:
            # Send the request
            with requests.Session() as s:
                resp = s.send(req, timeout=25)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            self._raise_if_error_is_authentication_error(e)
            logger.exception(f"Failed to make Microsoft Graph request. Response body: {e.response.json()}")
            raise e

    # ---------------------------
    # Listing & single fetch
    # ---------------------------
    def _format_dt_for_graph(self, dt: datetime) -> str:
        """Return RFC3339 UTC like 2025-08-20T15:30:00Z (seconds precision)."""
        if dt.tzinfo is None:
            # If you *know* your inputs are always aware, you can assert instead.
            raise ValueError("Naive datetime passed to _format_dt_for_graph")
        return dt.astimezone(python_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _calendar_base_url(self) -> str:
        """
        Return the base path for calendar-scoped calls.
        If platform_uuid is set, scope to that calendar; otherwise use the user's primary.
        """
        if self.calendar.platform_uuid:
            return f"{self.GRAPH_BASE}/me/calendars/{self.calendar.platform_uuid}"
        return f"{self.GRAPH_BASE}/me"

    def _list_events(self, access_token: str) -> List[dict]:
        """
        Use /me/calendarView to enumerate events (including expanded recurrences)
        within the time window, with paging via @odata.nextLink.
        """
        start = self._format_dt_for_graph(self.time_window_start)
        end = self._format_dt_for_graph(self.time_window_end)

        base_url = f"{self._calendar_base_url()}/calendarView"
        params = {
            "startDateTime": start,
            "endDateTime": end,
            # Order helps with pagination sanity; Graph supports this on calendarView.
            "$orderby": "start/dateTime",
            # Keep payload lean but include what we need. We try to get the joinUrl via $expand.
            "$select": self.CALENDAR_EVENT_SELECT_FIELDS,
            # You can tune page size with $top (Graph may cap it). We let Graph decide for reliability.
            # "$top": "200",
        }

        events: list[dict] = []
        data = self._make_graph_request(base_url, access_token, params)

        events.extend(data.get("value", []))
        next_link = data.get("@odata.nextLink")

        while next_link:
            # next_link already includes all parameters and skip tokens
            data = self._make_graph_request(next_link, access_token)
            events.extend(data.get("value", []))
            next_link = data.get("@odata.nextLink")

        logger.info("Fetched %d events from Microsoft Graph", len(events))
        return events

    def _get_event_by_id(self, event_id: str, access_token: str) -> Optional[dict]:
        """
        Fetch a specific event by id. If it's been deleted, Graph returns 404.
        """
        url = f"{self._calendar_base_url()}/events/{event_id}"
        params = {
            "$select": self.CALENDAR_EVENT_SELECT_FIELDS,
        }
        try:
            return self._make_graph_request(url, access_token, params)
        except Exception as e:
            if _exception_is_404(e):
                logger.info("Event %s not found in Microsoft Graph", event_id)
                return None  # Event was deleted
            raise

    # ---------------------------
    # Mapping helpers
    # ---------------------------
    def _parse_ms_datetime(self, dt_str: str, tz_name: str | None) -> datetime:
        """
        Parse Microsoft Graph dateTime strings.

        - Works with 'Z', offsets, and >6-digit fractional seconds.
        - If the string is naive, attach tz_name (usually 'UTC' when using
        Prefer: outlook.timezone="UTC"), else default to UTC.
        """
        if not dt_str:
            raise ValueError("Empty dateTime")

        dt = dateutil.parser.isoparse(dt_str)  # handles Z, offsets, long fractions

        if dt.tzinfo is None:
            if tz_name:
                try:
                    dt = dt.replace(tzinfo=ZoneInfo(tz_name))
                except Exception:
                    # Fallback if tz_name is unknown on the system
                    dt = dt.replace(tzinfo=python_timezone.utc)
            else:
                dt = dt.replace(tzinfo=python_timezone.utc)

        return dt

    def _extract_meeting_url(self, ev: dict) -> Optional[str]:
        """
        Try to extract a join URL for online meetings.
        Priority:
          1) onlineMeeting.joinUrl (expanded)
          2) onlineMeetingUrl (legacy)
          3) (fallback) None
        """
        online_meeting = ev.get("onlineMeeting") or {}
        join_url = extract_meeting_url_from_text(online_meeting.get("joinUrl"))
        if join_url:
            return join_url
        legacy = extract_meeting_url_from_text(ev.get("onlineMeetingUrl"))
        if legacy:
            return legacy

        # Try to extract a meeting url from the subject
        url_from_subject = extract_meeting_url_from_text(ev.get("subject", ""))
        if url_from_subject:
            return url_from_subject
        # Try to extract a meeting url from the body
        url_from_body = extract_meeting_url_from_text(ev.get("body", {}).get("content", ""))
        if url_from_body:
            return url_from_body
        return None

    def _truncate_large_text_fields_in_ms_event(self, ms_event: dict) -> dict:
        """Truncate large text fields in a Microsoft Graph event. Return a copy of the event with the fields truncated."""
        event_copy = copy.deepcopy(ms_event)
        if event_copy.get("body") and event_copy.get("body").get("content"):
            event_copy["body"]["content"] = event_copy.get("body").get("content")[:8000]
        return event_copy

    def _remote_event_to_calendar_event_data(self, ms_event: dict) -> dict:
        """Convert Microsoft Graph event into our CalendarEvent fields."""
        start_info = ms_event.get("start") or {}
        end_info = ms_event.get("end") or {}

        start_time = self._parse_ms_datetime(start_info.get("dateTime"), start_info.get("timeZone"))
        end_time = self._parse_ms_datetime(end_info.get("dateTime"), end_info.get("timeZone"))

        # Attendees
        attendees_out: list[dict] = []
        for att in ms_event.get("attendees", []) or []:
            email_obj = att.get("emailAddress") or {}
            attendees_out.append(
                {
                    "email": email_obj.get("address"),
                    "name": email_obj.get("name"),
                }
            )

        meeting_url = self._extract_meeting_url(ms_event)

        return {
            "platform_uuid": ms_event.get("id"),
            "meeting_url": meeting_url,
            "name": ms_event.get("subject"),
            "start_time": start_time,
            "end_time": end_time,
            "attendees": attendees_out,
            "raw": self._truncate_large_text_fields_in_ms_event(ms_event),
            "is_deleted": bool(ms_event.get("isCancelled")),
            "ical_uid": ms_event.get("iCalUId"),
        }
