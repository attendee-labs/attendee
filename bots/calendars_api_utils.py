from django.db import IntegrityError, transaction

from .models import Calendar, CalendarStates
from .serializers import CreateCalendarSerializer


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
            return None, {"non_field_errors": ["An error occurred while creating the calendar: " + str(e)]}
    except Exception as e:
        return None, {"non_field_errors": ["An unexpected error occurred while creating the calendar: " + str(e)]}
