import logging

from django.core.exceptions import ValidationError
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .app_session_api_utils import create_app_session
from .app_session_serializers import AppSessionSerializer, CreateAppSessionSerializer
from .authentication import ApiKeyAuthentication
from .bots_api_utils import BotCreationSource, send_sync_command
from .launch_bot_utils import launch_bot
from .models import (
    Bot,
    BotEventManager,
    BotEventTypes,
    BotStates,
    Recording,
    SessionTypes,
)
from .serializers import RecordingSerializer

logger = logging.getLogger(__name__)

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

NewlyCreatedAppSessionExample = OpenApiExample(
    "New app session",
    value={
        "id": "app_sess_weIAju4OXNZkDTpZ",
        "zoom_rtms_stream_id": "1234567890",
        "state": "joining",
        "events": [{"type": "join_requested", "created_at": "2024-01-18T12:34:56Z"}],
        "transcription_state": "not_started",
        "recording_state": "not_started",
    },
)


@extend_schema(exclude=True)
class NotFoundView(APIView):
    def get(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def handle_request(self, request, *args, **kwargs):
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)


class AppSessionCreateView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Create App Session",
        summary="Create a new app session",
        description="After being created, the app session will connect to the specified media stream.",
        request=CreateAppSessionSerializer,
        responses={
            201: OpenApiResponse(
                response=AppSessionSerializer,
                description="App session created successfully",
                examples=[NewlyCreatedAppSessionExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["App Sessions"],
    )
    def post(self, request):
        app_session, error = create_app_session(data=request.data, source=BotCreationSource.API, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        # If this is a scheduled bot, we don't want to launch it yet.
        if app_session.state == BotStates.CONNECTING:
            launch_bot(app_session)

        return Response(AppSessionSerializer(app_session).data, status=status.HTTP_201_CREATED)


class AppSessionEndView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="End App Session",
        summary="End an app session",
        description="Causes the app session to end.",
        responses={
            200: OpenApiResponse(
                response=AppSessionSerializer,
                description="Successfully requested to end app session",
            ),
            404: OpenApiResponse(description="App session not found"),
        },
        tags=["App Sessions"],
    )
    def post(self, request):
        try:
            rtms_stream_id = request.data.get("zoom_rtms").get("rtms_stream_id")
            app_session = Bot.objects.get(zoom_rtms_stream_id=rtms_stream_id, project=request.auth.project)

            BotEventManager.create_event(app_session, BotEventTypes.APP_SESSION_DISCONNECT_REQUESTED)

            send_sync_command(app_session)

            return Response(AppSessionSerializer(app_session).data, status=status.HTTP_200_OK)
        except ValidationError as e:
            logging.error(f"Error ending app session: {str(e)} (app_session_id={app_session.object_id})")
            return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        except Bot.DoesNotExist:
            return Response({"error": "App session not found"}, status=status.HTTP_404_NOT_FOUND)


class AppSessionMediaView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get App Session Media",
        summary="Get the media for an app session",
        description="Returns a short-lived S3 URL for the media of the app session.",
        responses={
            200: OpenApiResponse(
                response=RecordingSerializer,
                description="Short-lived S3 URL for the recording",
            )
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="App Session ID",
                examples=[OpenApiExample("App Session ID Example", value="app_session_xxxxxxxxxxx")],
            ),
        ],
        tags=["App Sessions"],
    )
    def get(self, request, object_id):
        try:
            app_session = Bot.objects.get(object_id=object_id, project=request.auth.project, session_type=SessionTypes.APP_SESSION)

            recording = Recording.objects.filter(bot=app_session, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No media found for app session"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            recording_file = recording.file
            if not recording_file:
                return Response(
                    {"error": "No media file found for app session"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            return Response(RecordingSerializer(recording).data)

        except Bot.DoesNotExist:
            return Response({"error": "App session not found"}, status=status.HTTP_404_NOT_FOUND)
