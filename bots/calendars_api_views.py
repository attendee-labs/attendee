from drf_spectacular.openapi import OpenApiResponse
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import ApiKeyAuthentication
from .calendars_api_utils import create_calendar, delete_calendar
from .models import Calendar, CalendarEvent
from .serializers import CalendarEventSerializer, CalendarSerializer, CreateCalendarSerializer, PatchCalendarSerializer
from .tasks.sync_calendar_task import enqueue_sync_calendar_task
from .throttling import ProjectPostThrottle

TokenHeaderParameter = [
    OpenApiParameter(
        name="Authorization",
        type=str,
        location=OpenApiParameter.HEADER,
        description="API key for authentication",
        required=True,
        default="Token YOUR_API_KEY_HERE",
    ),
    OpenApiParameter(
        name="Content-Type",
        type=str,
        location=OpenApiParameter.HEADER,
        description="Should always be application/json",
        required=True,
        default="application/json",
    ),
]

NewlyCreatedCalendarExample = OpenApiExample(
    "Newly Created Calendar",
    value={
        "id": "cal_abcdef1234567890",
        "platform": "google",
        "state": "connected",
        "metadata": {"tenant_id": "1234567890"},
        "deduplication_key": "user-abcd",
        "connection_failure_data": None,
        "created_at": "2025-01-13T10:30:00.123456Z",
        "updated_at": "2025-01-13T10:30:00.123456Z",
    },
    description="Example response when a calendar is successfully created",
)


class CalendarCursorPagination(CursorPagination):
    ordering = "-created_at"
    page_size = 25


class CalendarListCreateView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    pagination_class = CalendarCursorPagination
    serializer_class = CalendarSerializer

    @extend_schema(
        operation_id="List Calendars",
        summary="List calendars",
        description="Returns a list of calendars for the authenticated project. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=CalendarSerializer(many=True),
                description="List of calendars",
            ),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Cursor for pagination",
                required=False,
            ),
            OpenApiParameter(
                name="deduplication_key",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter calendars by deduplication key",
                required=False,
                examples=[OpenApiExample("Deduplication Key Example", value="user-abcd")],
            ),
        ],
        tags=["Calendars"],
    )
    def get(self, request):
        calendars = Calendar.objects.filter(project=request.auth.project)

        # Apply deduplication_key filter if provided
        deduplication_key = request.query_params.get("deduplication_key")
        if deduplication_key is not None:
            calendars = calendars.filter(deduplication_key=deduplication_key)

        calendars = calendars.order_by("-created_at")

        # Let the pagination class handle the rest
        page = self.paginate_queryset(calendars)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(calendars, many=True)
        return Response(serializer.data)

    @extend_schema(
        operation_id="Create Calendar",
        summary="Create a new calendar",
        description="After being created, the calendar will be connected to the specified calendar platform.",
        request=CreateCalendarSerializer,
        responses={
            201: OpenApiResponse(
                response=CalendarSerializer,
                description="Calendar created successfully",
                examples=[NewlyCreatedCalendarExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Calendars"],
    )
    def post(self, request):
        calendar, error = create_calendar(data=request.data, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        # Immediately sync the calendar
        enqueue_sync_calendar_task(calendar)

        return Response(CalendarSerializer(calendar).data, status=status.HTTP_201_CREATED)


class CalendarDetailPatchDeleteView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Calendar",
        summary="Get calendar details",
        description="Returns the details of a specific calendar.",
        responses={
            200: OpenApiResponse(
                response=CalendarSerializer,
                description="Calendar details",
                examples=[NewlyCreatedCalendarExample],
            ),
            404: OpenApiResponse(description="Calendar not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Calendar ID",
                examples=[OpenApiExample("Calendar ID Example", value="cal_abcdef1234567890")],
            ),
        ],
        tags=["Calendars"],
    )
    def get(self, request, object_id):
        try:
            calendar = Calendar.objects.get(object_id=object_id, project=request.auth.project)
            return Response(CalendarSerializer(calendar).data, status=status.HTTP_200_OK)
        except Calendar.DoesNotExist:
            return Response({"error": "Calendar not found"}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        operation_id="Update Calendar",
        summary="Update calendar",
        description="Updates calendar credentials (client_secret, refresh_token) or metadata.",
        request=PatchCalendarSerializer,
        responses={
            200: OpenApiResponse(
                response=CalendarSerializer,
                description="Calendar updated successfully",
                examples=[NewlyCreatedCalendarExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Calendar not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Calendar ID",
                examples=[OpenApiExample("Calendar ID Example", value="cal_abcdef1234567890")],
            ),
        ],
        tags=["Calendars"],
    )
    def patch(self, request, object_id):
        try:
            calendar = Calendar.objects.get(object_id=object_id, project=request.auth.project)
        except Calendar.DoesNotExist:
            return Response({"error": "Calendar not found"}, status=status.HTTP_404_NOT_FOUND)

        # Validate the request data
        serializer = PatchCalendarSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated_data = serializer.validated_data

        # Update metadata if provided
        if "metadata" in validated_data:
            calendar.metadata = validated_data["metadata"]

        # Update credentials if provided
        client_secret = validated_data.get("client_secret")
        refresh_token = validated_data.get("refresh_token")

        if client_secret is not None or refresh_token is not None:
            # Get existing credentials
            existing_credentials = calendar.get_credentials() or {}

            # Update only the provided fields
            if client_secret is not None:
                existing_credentials["client_secret"] = client_secret
            if refresh_token is not None:
                existing_credentials["refresh_token"] = refresh_token

            # Save updated credentials
            calendar.set_credentials(existing_credentials)

        calendar.save()

        # Request an immediate sync of the calendar
        enqueue_sync_calendar_task(calendar)

        return Response(CalendarSerializer(calendar).data, status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="Delete Calendar",
        summary="Delete calendar",
        description="Permanently deletes a calendar and all associated data.",
        responses={
            200: OpenApiResponse(description="Calendar deleted successfully"),
            404: OpenApiResponse(description="Calendar not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Calendar ID",
                examples=[OpenApiExample("Calendar ID Example", value="cal_abcdef1234567890")],
            ),
        ],
        tags=["Calendars"],
    )
    def delete(self, request, object_id):
        try:
            calendar = Calendar.objects.get(object_id=object_id, project=request.auth.project)
            success, error = delete_calendar(calendar)
            if error:
                return Response(error, status=status.HTTP_400_BAD_REQUEST)
            return Response(status=status.HTTP_200_OK)
        except Calendar.DoesNotExist:
            return Response({"error": "Calendar not found"}, status=status.HTTP_404_NOT_FOUND)


class CalendarEventCursorPagination(CursorPagination):
    ordering = "-updated_at"
    page_size = 100


class CalendarEventListView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    pagination_class = CalendarEventCursorPagination
    serializer_class = CalendarEventSerializer

    @extend_schema(
        operation_id="List Calendar Events",
        summary="List calendar events",
        description="Returns a list of calendar events for the authenticated project. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=CalendarEventSerializer(many=True),
                description="List of calendar events",
            ),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Cursor for pagination",
                required=False,
            ),
            OpenApiParameter(
                name="calendar_id",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter events by calendar ID",
                required=False,
                examples=[OpenApiExample("Calendar ID Example", value="cal_abcdef1234567890")],
            ),
            OpenApiParameter(
                name="calendar_deduplication_key",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter events by calendar deduplication key",
                required=False,
                examples=[OpenApiExample("Deduplication Key Example", value="user-abcd")],
            ),
            OpenApiParameter(
                name="updated_after",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter events updated after this timestamp (ISO 8601 format)",
                required=False,
                examples=[OpenApiExample("Updated After Example", value="2025-01-13T10:30:00Z")],
            ),
        ],
        tags=["Calendars"],
    )
    def get(self, request):
        # Start with all events for calendars in the authenticated project
        events = CalendarEvent.objects.filter(calendar__project=request.auth.project).select_related("calendar")

        # Apply calendar_id filter if provided
        calendar_id = request.query_params.get("calendar_id")
        if calendar_id is not None:
            events = events.filter(calendar__object_id=calendar_id)

        # Apply calendar_deduplication_key filter if provided
        calendar_deduplication_key = request.query_params.get("calendar_deduplication_key")
        if calendar_deduplication_key is not None:
            events = events.filter(calendar__deduplication_key=calendar_deduplication_key)

        # Apply updated_after filter if provided
        updated_after = request.query_params.get("updated_after")
        if updated_after is not None:
            try:
                from django.utils.dateparse import parse_datetime

                updated_after_dt = parse_datetime(updated_after)
                if updated_after_dt is None:
                    return Response({"error": "Invalid updated_after format. Use ISO 8601 format (e.g., 2025-01-13T10:30:00Z)"}, status=status.HTTP_400_BAD_REQUEST)
                events = events.filter(updated_at__gte=updated_after_dt)
            except ValueError:
                return Response({"error": "Invalid updated_after format. Use ISO 8601 format (e.g., 2025-01-13T10:30:00Z)"}, status=status.HTTP_400_BAD_REQUEST)

        events = events.order_by("-created_at")

        # Let the pagination class handle the rest
        page = self.paginate_queryset(events)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(events, many=True)
        return Response(serializer.data)
