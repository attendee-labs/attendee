import logging
import uuid

from django.db import IntegrityError, transaction

from .models import Bot, BotStates, Calendar, CalendarStates, Project
from .serializers import CreateCalendarSerializer

logger = logging.getLogger(__name__)


def create_calendar(data, project):
    """
    Create a new calendar for the given project.

    Args:
        data: Dictionary containing calendar creation data
        project: Project instance to associate the calendar with

    Returns:
        tuple: (calendar_instance, error_dict)
               Returns (Calendar, None) on success
               Returns (None, error_dict) on failure
    """
    # Validate the input data
    serializer = CreateCalendarSerializer(data=data)
    if not serializer.is_valid():
        return None, serializer.errors

    validated_data = serializer.validated_data

    try:
        with transaction.atomic():
            # Create the calendar instance
            calendar = Calendar(project=project, platform=validated_data["platform"], client_id=validated_data["client_id"], state=CalendarStates.CONNECTED, metadata=validated_data.get("metadata"), deduplication_key=validated_data.get("deduplication_key"), platform_uuid=validated_data.get("platform_uuid"))

            # Set encrypted credentials (client_secret and refresh_token)
            credentials = {"client_secret": validated_data["client_secret"], "refresh_token": validated_data["refresh_token"]}
            calendar.set_credentials(credentials)

            # Save the calendar
            calendar.save()

            return calendar, None

    except IntegrityError as e:
        # Handle any database integrity errors (e.g., duplicate deduplication key)
        if "deduplication_key" in str(e).lower():
            return None, {"deduplication_key": ["A calendar with this deduplication key already exists in this project."]}
        else:
            error_id = str(uuid.uuid4())
            logger.error(f"Error creating calendar (error_id={error_id}): {e}")
            return None, {"non_field_errors": ["An error occurred while creating the calendar. Error ID: " + error_id]}
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error creating calendar (error_id={error_id}): {e}")
        return None, {"non_field_errors": ["An unexpected error occurred while creating the calendar. Error ID: " + error_id]}


def remove_bots_from_calendar(calendar: Calendar, project: Project):
    """
    Remove all scheduled bots from a calendar using bulk delete.

    Args:
        calendar: Calendar instance to remove bots from
    """

    # Bulk delete all scheduled bots for this calendar
    try:
        deleted_count, _ = Bot.objects.filter(calendar_event__calendar=calendar, state=BotStates.SCHEDULED, project=project).delete()

        logger.info(f"remove_bots_from_calendar deleted {deleted_count} scheduled bots from calendar {calendar.id}")
    except Exception as e:
        logger.exception(f"remove_bots_from_calendar failed to delete scheduled bots from calendar {calendar.id}: {e}")


def delete_calendar(calendar: Calendar) -> tuple[bool, dict]:
    """
    Delete a calendar and all associated data.

    Args:
        calendar: Calendar instance to delete
    """
    try:
        with transaction.atomic():
            remove_bots_from_calendar(calendar=calendar, project=calendar.project)
            calendar.delete()

        return True, None
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error deleting calendar (error_id={error_id}): {e}")
        return False, {"non_field_errors": ["An unexpected error occurred while deleting the calendar. Error ID: " + error_id]}
