import logging
import os
import time

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import ApiKeyAuthentication
from .bots_api_utils import BotCreationSource, create_bot, create_bot_chat_message_request, create_bot_media_request_for_image, delete_bot, patch_bot, patch_bot_transcription_settings, patch_bot_voice_agent_settings, send_sync_command
from .launch_bot_utils import launch_bot
from .meeting_url_utils import meeting_type_from_url
from .models import (
    AsyncTranscription,
    AsyncTranscriptionStates,
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    BotStates,
    ChatMessage,
    Credentials,
    MediaBlob,
    MeetingTypes,
    Participant,
    ParticipantEvent,
    Recording,
    RecordingViews,
    Utterance,
)
from .serializers import (
    AsyncTranscriptionSerializer,
    BotChatMessageRequestSerializer,
    BotImageSerializer,
    BotSerializer,
    ChatMessageSerializer,
    CreateAsyncTranscriptionSerializer,
    CreateBotSerializer,
    ParticipantEventSerializer,
    ParticipantSerializer,
    PatchBotSerializer,
    PatchBotVoiceAgentSettingsSerializer,
    RecordingSerializer,
    SpeechSerializer,
    TranscriptUtteranceSerializer,
)
from .tasks import process_async_transcription
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

LeavingBotExample = OpenApiExample(
    "Leaving Bot",
    value={
        "id": "bot_weIAju4OXNZkDTpZ",
        "meeting_url": "https://zoom.us/j/123?pwd=456",
        "state": "leaving",
        "events": [
            {"type": "join_requested", "created_at": "2024-01-18T12:34:56Z"},
            {"type": "joined_meeting", "created_at": "2024-01-18T12:35:00Z"},
            {"type": "leave_requested", "created_at": "2024-01-18T13:34:56Z"},
        ],
        "transcription_state": "in_progress",
        "recording_state": "in_progress",
    },
    description="Example response when requesting a bot to leave",
)

NewlyCreatedBotExample = OpenApiExample(
    "New bot",
    value={
        "id": "bot_weIAju4OXNZkDTpZ",
        "meeting_url": "https://zoom.us/j/123?pwd=456",
        "state": "joining",
        "events": [{"type": "join_requested", "created_at": "2024-01-18T12:34:56Z"}],
        "transcription_state": "not_started",
        "recording_state": "not_started",
    },
    description="Example response when creating a new bot",
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


class BotCursorPagination(CursorPagination):
    ordering = "created_at"
    page_size = 25


class BotListCreateView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    pagination_class = BotCursorPagination
    serializer_class = BotSerializer

    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="List Bots",
        summary="List bots in a project",
        description="Returns a list of bots for the authenticated project. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer(many=True),
                description="List of bots",
            )
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="meeting_url",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter bots by meeting URL",
                required=False,
                examples=[OpenApiExample("Meeting URL Example", value="https://zoom.us/j/123456789")],
            ),
            OpenApiParameter(
                name="deduplication_key",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter bots by deduplication key",
                required=False,
                examples=[OpenApiExample("Deduplication Key Example", value="my-unique-bot-key")],
            ),
            OpenApiParameter(
                name="states",
                type={"type": "array", "items": {"type": "string", "enum": list(BotStates._get_state_to_api_code_mapping().values())}},
                location=OpenApiParameter.QUERY,
                description="Filter bots by state. Can specify multiple states.",
                required=False,
            ),
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Cursor for pagination",
                required=False,
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request):
        # Start with all bots for the project
        bots_query = Bot.objects.filter(project=request.auth.project)

        # Filter by meeting_url if provided
        meeting_url = request.query_params.get("meeting_url")
        if meeting_url:
            bots_query = bots_query.filter(meeting_url=meeting_url)

        # Filter by deduplication_key if provided
        deduplication_key = request.query_params.get("deduplication_key")
        if deduplication_key:
            bots_query = bots_query.filter(deduplication_key=deduplication_key)

        # Filter by states if provided
        states = request.query_params.getlist("states")
        if states:
            # Convert API code strings to state integer values
            state_values = []
            for state_api_code in states:
                state_value = BotStates.api_code_to_state(state_api_code)
                if state_value is not None:
                    state_values.append(state_value)
                else:
                    return Response({"error": f"Invalid state: {state_api_code}. Valid states are: {', '.join(BotStates.state_to_api_code(state) for state in BotStates)}"}, status=status.HTTP_400_BAD_REQUEST)

            if state_values:
                bots_query = bots_query.filter(state__in=state_values)

        # Apply ordering for cursor pagination
        bots = bots_query.order_by("created_at")

        # Let the pagination class handle the rest
        page = self.paginate_queryset(bots)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(bots, many=True)
        return Response(serializer.data)

    @extend_schema(
        operation_id="Create Bot",
        summary="Create a new bot",
        description="After being created, the bot will attempt to join the specified meeting.",
        request=CreateBotSerializer,
        responses={
            201: OpenApiResponse(
                response=BotSerializer,
                description="Bot created successfully",
                examples=[NewlyCreatedBotExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Bots"],
    )
    def post(self, request):
        bot, error = create_bot(data=request.data, source=BotCreationSource.API, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        # If this is a scheduled bot, we don't want to launch it yet.
        if bot.state == BotStates.JOINING:
            launch_bot(bot)

        return Response(BotSerializer(bot).data, status=status.HTTP_201_CREATED)


class SpeechView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="Output speech",
        summary="Output speech",
        description="Causes the bot to speak a message in the meeting.",
        request=SpeechSerializer,
        responses={
            200: OpenApiResponse(description="Speech request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        # Get the bot
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

        # Validate the request data
        serializer = SpeechSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Check if bot is in a state that can play media
        if not BotEventManager.is_state_that_can_play_media(bot.state):
            return Response(
                {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check for Google TTS credentials. This is currently the only supported text-to-speech provider.
        google_tts_credentials = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.GOOGLE_TTS).first()

        if not google_tts_credentials:
            settings_url = request.build_absolute_uri(reverse("bots:project-credentials", kwargs={"object_id": bot.project.object_id}))
            return Response(
                {"error": f"Google Text-to-Speech credentials are required. Please add credentials at {settings_url}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the media request
        BotMediaRequest.objects.create(
            bot=bot,
            text_to_speak=serializer.validated_data["text"],
            text_to_speech_settings=serializer.validated_data["text_to_speech_settings"],
            media_type=BotMediaRequestMediaTypes.AUDIO,
        )

        # Send sync command to notify bot of new media request
        send_sync_command(bot, "sync_media_requests")

        return Response(status=status.HTTP_200_OK)


class OutputVideoView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="Output video",
        summary="Output video",
        description="Causes the bot to output a video in the meeting.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL of the video to output. Must be a valid URL to an mp4 file.",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            }
        },
        responses={
            200: OpenApiResponse(description="Video request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        # Get the bot
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

        # Get which type of meeting the bot is in
        meeting_type = meeting_type_from_url(bot.meeting_url)
        print(f"meeting_type = {meeting_type}")
        
        # Validate the request data
        url = request.data.get("url")
        if not url:
            return Response({"error": "URL is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not url.startswith("https://"):
            return Response({"error": "URL must start with https://"}, status=status.HTTP_400_BAD_REQUEST)
        if not url.endswith(".mp4"):
            return Response({"error": "URL must end with .mp4"}, status=status.HTTP_400_BAD_REQUEST)

        # Check if bot is in a state that can play media
        if not BotEventManager.is_state_that_can_play_media(bot.state):
            return Response(
                {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if bot.media_requests.filter(state=BotMediaRequestStates.PLAYING).exists():
            return Response({"error": "Bot is already playing media. Please wait for it to finish."}, status=status.HTTP_400_BAD_REQUEST)

        # Create the media request
        BotMediaRequest.objects.create(
            bot=bot,
            media_type=BotMediaRequestMediaTypes.VIDEO,
            media_url=url,
        )

        # Send sync command to notify bot of new media request
        send_sync_command(bot, "sync_media_requests")

        return Response(status=status.HTTP_200_OK)


class OutputAudioView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="Output Audio",
        summary="Output audio",
        description="Causes the bot to output audio in the meeting.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [ct[0] for ct in MediaBlob.VALID_AUDIO_CONTENT_TYPES],
                    },
                    "data": {
                        "type": "string",
                        "description": "Base64 encoded audio data",
                    },
                },
                "required": ["type", "data"],
            }
        },
        responses={
            200: OpenApiResponse(description="Audio request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            # Validate request data
            if "type" not in request.data or "data" not in request.data:
                return Response(
                    {"error": "Both type and data are required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            content_type = request.data["type"]
            if content_type not in [ct[0] for ct in MediaBlob.VALID_AUDIO_CONTENT_TYPES]:
                return Response(
                    {"error": "Invalid audio content type"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                # Decode base64 data
                import base64

                audio_data = base64.b64decode(request.data["data"])
            except Exception:
                return Response(
                    {"error": "Invalid base64 encoded data"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Get the bot
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            if not BotEventManager.is_state_that_can_play_media(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                # Create or get existing MediaBlob
                media_blob = MediaBlob.get_or_create_from_blob(project=request.auth.project, blob=audio_data, content_type=content_type)
            except Exception as e:
                error_message_first_line = str(e).split("\n")[0]
                logging.error(f"Error creating audio blob: {error_message_first_line} (content_type={content_type}, bot_id={object_id})")
                return Response({"error": f"Error creating the audio blob. Are you sure it's a valid {content_type} file?", "raw_error": error_message_first_line}, status=status.HTTP_400_BAD_REQUEST)

            # Create BotMediaRequest
            BotMediaRequest.objects.create(
                bot=bot,
                media_blob=media_blob,
                media_type=BotMediaRequestMediaTypes.AUDIO,
            )

            # Send sync command
            send_sync_command(bot, "sync_media_requests")

            return Response(status=status.HTTP_200_OK)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class OutputImageView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="Output Image",
        summary="Output image",
        description="Causes the bot to output an image in the meeting.",
        request=BotImageSerializer,
        responses={
            200: OpenApiResponse(description="Image request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            # Get the bot
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            if not BotEventManager.is_state_that_can_play_media(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if bot.media_requests.filter(media_type=BotMediaRequestMediaTypes.VIDEO, state=BotMediaRequestStates.PLAYING).exists():
                return Response({"error": "Bot is already playing a video. Please wait for it to finish."}, status=status.HTTP_400_BAD_REQUEST)

            # Validate request data
            bot_image = BotImageSerializer(data=request.data)
            if not bot_image.is_valid():
                return Response(bot_image.errors, status=status.HTTP_400_BAD_REQUEST)

            try:
                create_bot_media_request_for_image(bot, bot_image.validated_data)
            except ValidationError as e:
                return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)

            # Send sync command
            send_sync_command(bot, "sync_media_requests")

            return Response(status=status.HTTP_200_OK)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class DeleteDataView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Delete Bot Data",
        summary="Delete bot data",
        description="Permanently deletes all data associated with this bot, including recordings, transcripts, and participant information. Metadata is not deleted. This cannot be undone.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Data successfully deleted",
            ),
            400: OpenApiResponse(description="Bot is not in a valid state for data deletion"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            logging.info(f"Deleting data for bot {bot.object_id}")
            bot.delete_data()
            logging.info(f"Data deleted for bot {bot.object_id}")
            return Response(BotSerializer(bot).data, status=status.HTTP_200_OK)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logging.error(f"Error deleting bot data: {str(e)} (bot_id={object_id})")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )


class BotLeaveView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Leave Meeting",
        summary="Leave a meeting",
        description="Causes the bot to leave the meeting.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Successfully requested to leave meeting",
                examples=[LeavingBotExample],
            ),
            400: OpenApiResponse(description="Bot is not in a valid state to leave the meeting"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            BotEventManager.create_event(bot, BotEventTypes.LEAVE_REQUESTED, event_sub_type=BotEventSubTypes.LEAVE_REQUESTED_USER_REQUESTED)

            send_sync_command(bot)

            return Response(BotSerializer(bot).data, status=status.HTTP_200_OK)
        except ValidationError as e:
            logging.error(f"Error leaving meeting: {str(e)} (bot_id={object_id})")
            return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class RecordingView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot Recording",
        summary="Get the recording for a bot",
        description="Returns a short-lived S3 URL for the recording of the bot.",
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
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            recording_file = recording.file
            if not recording_file:
                return Response(
                    {"error": "No recording file found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            return Response(RecordingSerializer(recording).data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class TranscriptView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot Transcript",
        summary="Get the transcript for a bot",
        description="If the meeting is still in progress, this returns the transcript so far.",
        responses={
            200: OpenApiResponse(
                response=TranscriptUtteranceSerializer(many=True),
                description="List of transcribed utterances",
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
            OpenApiParameter(
                name="updated_after",
                type={"type": "string", "format": "ISO 8601 datetime"},
                location=OpenApiParameter.QUERY,
                description="Only return transcript entries updated or created after this time. Useful when polling for updates to the transcript.",
                required=False,
                examples=[OpenApiExample("DateTime Example", value="2024-01-18T12:34:56Z")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            async_transcription = None
            if request.query_params.get("async_transcription_id"):
                async_transcription = recording.async_transcriptions.get(
                    object_id=request.query_params.get("async_transcription_id"),
                )
                if async_transcription.state != AsyncTranscriptionStates.COMPLETE:
                    return Response({"error": f"Async transcription {async_transcription.object_id} is not complete. It is in state {AsyncTranscriptionStates.state_to_api_code(async_transcription.state)}"}, status=status.HTTP_400_BAD_REQUEST)

            # Get all utterances with transcriptions, sorted by timeline
            utterances_query = Utterance.objects.select_related("participant").filter(recording=recording, transcription__isnull=False, async_transcription=async_transcription)

            # Apply updated_after filter if provided
            updated_after = request.query_params.get("updated_after")
            if updated_after:
                try:
                    updated_after_datetime = parse_datetime(str(updated_after))
                except Exception:
                    updated_after_datetime = None

                if not updated_after_datetime:
                    return Response(
                        {"error": "Invalid updated_after format. Use ISO 8601 format (e.g., 2024-01-18T12:34:56Z)"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                utterances_query = utterances_query.filter(updated_at__gt=updated_after_datetime)

            # Apply ordering
            utterances = utterances_query.order_by("timestamp_ms")

            # Format the response, skipping empty transcriptions
            transcript_data = [
                {
                    "speaker_name": utterance.participant.full_name,
                    "speaker_uuid": utterance.participant.uuid,
                    "speaker_user_uuid": utterance.participant.user_uuid,
                    "speaker_is_host": utterance.participant.is_host,
                    "timestamp_ms": utterance.timestamp_ms,
                    "duration_ms": utterance.duration_ms,
                    "transcription": utterance.transcription,
                }
                for utterance in utterances
                if utterance.transcription.get("transcript", "")
            ]

            serializer = TranscriptUtteranceSerializer(transcript_data, many=True)
            return Response(serializer.data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)
        except AsyncTranscription.DoesNotExist:
            return Response({"error": "Async Transcription not found"}, status=status.HTTP_404_NOT_FOUND)

    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            if not bot.project.organization.is_async_transcription_enabled:
                return Response({"error": "Async transcription is not enabled for your account."}, status=status.HTTP_400_BAD_REQUEST)

            if not bot.record_async_transcription_audio_chunks():
                return Response({"error": "Cannot generate async transcription because you did not enable recording_settings.record_async_transcription_audio_chunks when you created the bot."}, status=status.HTTP_400_BAD_REQUEST)

            if bot.state != BotStates.ENDED:
                return Response({"error": "Cannot create async transcription because bot is not in state ended. It is in state " + BotStates.state_to_api_code(bot.state)}, status=status.HTTP_400_BAD_REQUEST)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if not recording.audio_chunks.exclude(audio_blob=b"").exists():
                return Response({"error": "Cannot create async transcription because the per-speaker audio data has been deleted or was never created."}, status=status.HTTP_400_BAD_REQUEST)

            existing_async_transcription_count = AsyncTranscription.objects.filter(
                recording=recording,
            ).count()
            # We only allow a max of 4 async transcriptions per recording
            if existing_async_transcription_count >= 4:
                return Response({"error": "You cannot have more than 4 async transcriptions per bot."}, status=status.HTTP_400_BAD_REQUEST)

            serializer = CreateAsyncTranscriptionSerializer(data={"transcription_settings": request.data.get("transcription_settings")})
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

            async_transcription = AsyncTranscription.objects.create(recording=recording, settings=serializer.validated_data)

            # Create celery task to process the async transcription
            process_async_transcription.delay(async_transcription.id)

            return Response(AsyncTranscriptionSerializer(async_transcription).data)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        operation_id="Delete Bot Transcript",
        summary="Delete the transcript for a bot",
        description="Deletes transcript utterances. If async_transcription_id is provided, deletes utterances for that async transcription. Otherwise, deletes the default (real-time) transcription utterances.",
        responses={
            200: OpenApiResponse(
                description="Transcript deleted successfully",
            ),
            404: OpenApiResponse(description="Bot, recording, or async transcription not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
            OpenApiParameter(
                name="async_transcription_id",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Optional. If provided, deletes the utterances for the specified async transcription. If not provided, deletes the default (real-time) transcription utterances.",
                required=False,
                examples=[OpenApiExample("Async Transcription ID Example", value="tran_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def delete(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            async_transcription = None
            if request.query_params.get("async_transcription_id"):
                async_transcription = recording.async_transcriptions.get(
                    object_id=request.query_params.get("async_transcription_id"),
                )

            # Delete all utterances with transcriptions for the given async_transcription
            Utterance.objects.filter(recording=recording, async_transcription=async_transcription).delete()

            return Response(status=status.HTTP_200_OK)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)
        except AsyncTranscription.DoesNotExist:
            return Response({"error": "Async Transcription not found"}, status=status.HTTP_404_NOT_FOUND)


class BotDetailView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot",
        summary="Get the details for a bot",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Bot details",
                examples=[NewlyCreatedBotExample],
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            return Response(BotSerializer(bot).data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        operation_id="Patch Bot",
        summary="Update a bot",
        description="Updates a bot. Currently only the join_at and metadata fields can be updated. The join_at field can only be updated when the bot is in the scheduled state. The metadata field can be updated at any time.",
        request=PatchBotSerializer,
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Bot updated successfully",
            ),
            400: OpenApiResponse(description="Invalid input or bot cannot be updated"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def patch(self, request, object_id):
        # Get the bot
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

        # Use the utility function to patch the bot
        updated_bot, error = patch_bot(bot, request.data)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        return Response(BotSerializer(updated_bot).data, status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="Delete scheduled Bot",
        summary="Delete a scheduled bot",
        description="Deletes a scheduled bot.",
        responses={
            200: OpenApiResponse(description="Scheduled bot deleted successfully"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def delete(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

        # Use the utility function to delete the bot
        success, error = delete_bot(bot)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_200_OK)


class ChatMessageCursorPagination(CursorPagination):
    ordering = "created_at"
    page_size = 25


class ChatMessagesView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    pagination_class = ChatMessageCursorPagination
    serializer_class = ChatMessageSerializer

    @extend_schema(
        operation_id="Get Chat Messages",
        summary="Get chat messages sent in the meeting",
        description="If the meeting is still in progress, this returns the chat messages sent so far. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=ChatMessageSerializer(many=True),
                description="List of chat messages",
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
            OpenApiParameter(
                name="updated_after",
                type={"type": "string", "format": "ISO 8601 datetime"},
                location=OpenApiParameter.QUERY,
                description="Only return chat messages created after this time. Useful when polling for updates.",
                required=False,
                examples=[OpenApiExample("DateTime Example", value="2024-01-18T12:34:56Z")],
            ),
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Cursor for pagination",
                required=False,
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            # Get the bot and verify it belongs to the project
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            # Get optional updated_after parameter
            updated_after = request.query_params.get("updated_after")

            # Query messages for this bot
            messages_query = ChatMessage.objects.filter(bot=bot)

            # Filter by updated_after if provided
            if updated_after:
                try:
                    updated_after_datetime = parse_datetime(str(updated_after))
                except Exception:
                    updated_after_datetime = None

                if not updated_after_datetime:
                    return Response(
                        {"error": "Invalid updated_after format. Use ISO 8601 format (e.g., 2024-01-18T12:34:56Z)"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                messages_query = messages_query.filter(created_at__gt=updated_after_datetime)

            # Apply ordering - now using created_at for cursor pagination
            messages = messages_query.order_by("created_at")

            # Let the pagination class handle the rest
            page = self.paginate_queryset(messages)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(messages, many=True)
            return Response(serializer.data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class SendChatMessageView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="Send Chat Message",
        summary="Send a chat message",
        description="Causes the bot to send a chat message in the meeting.",
        request=BotChatMessageRequestSerializer,
        responses={
            200: OpenApiResponse(description="Chat message request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        # Get the bot
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

        # Validate the request data
        serializer = BotChatMessageRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Check if bot is in a state that can send chat messages
        if not BotEventManager.is_state_that_can_play_media(bot.state):
            return Response(
                {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot send chat messages"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated_data = serializer.validated_data

        # Create the chat message request
        try:
            create_bot_chat_message_request(bot, validated_data)

            # Send sync command to notify bot of new chat message request
            send_sync_command(bot, "sync_chat_message_requests")

            return Response(status=status.HTTP_200_OK)

        except ValidationError as e:
            logging.error(f"Error creating chat message request for bot {bot.object_id}: {str(e)}")
            return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logging.error(f"Error creating chat message request for bot {bot.object_id}: {str(e)}")
            return Response(
                {"error": "Failed to create chat message request"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class VoiceAgentSettingsView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Update Voice Agent Settings",
        summary="Update the voice agent settings for a bot",
        description="Updates the voice agent settings for a bot.",
        request=PatchBotVoiceAgentSettingsSerializer,
        responses={
            200: OpenApiResponse(description="Voice agent settings updated successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def patch(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            bot_updated, error = patch_bot_voice_agent_settings(bot, request.data)
            if error:
                return Response(error, status=status.HTTP_400_BAD_REQUEST)

            try:
                logging.info(f"Patching voice agent settings for bot {bot.object_id}")
                send_sync_command(bot, "sync_voice_agent_settings")
                return Response(status=status.HTTP_200_OK)
            except Exception as e:
                logging.error(f"Error patching voice agent settings for bot {bot.object_id}: {str(e)}")
                return Response(
                    {"error": "Failed to patch voice agent settings"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class TranscriptionSettingsView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(exclude=True)
    def patch(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            bot_updated, error = patch_bot_transcription_settings(bot, request.data)
            if error:
                return Response(error, status=status.HTTP_400_BAD_REQUEST)

            try:
                logging.info(f"Patching transcription settings for bot {bot.object_id}")
                send_sync_command(bot, "sync_transcription_settings")
                return Response(status=status.HTTP_200_OK)
            except Exception as e:
                logging.error(f"Error patching transcription settings for bot {bot.object_id}: {str(e)}")
                return Response(
                    {"error": "Failed to patch transcription settings"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class AdmitFromWaitingRoomView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(exclude=True)
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            # This functionality is only supported for zoom bots
            meeting_type = meeting_type_from_url(bot.meeting_url)
            if meeting_type != MeetingTypes.ZOOM:
                return Response({"error": "Admitting from waiting room is not supported for this meeting type"}, status=status.HTTP_400_BAD_REQUEST)

            # Check if bot is in a state that allows admitting from waiting room
            if not BotEventManager.is_state_that_can_admit_from_waiting_room(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot admit from waiting room"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Call the utility method on the bot instance to admit from waiting room
            try:
                logging.info(f"Admitting from waiting room for bot {bot.object_id}")
                send_sync_command(bot, "admit_from_waiting_room")
                return Response(status=status.HTTP_200_OK)
            except Exception as e:
                logging.error(f"Error admitting from waiting room for bot {bot.object_id}: {str(e)}")
                return Response(
                    {"error": "Failed to admit from waiting room"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class ChangeGalleryViewPageView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(exclude=True)
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            # This functionality is only supported for zoom bots
            meeting_type = meeting_type_from_url(bot.meeting_url)
            if meeting_type != MeetingTypes.ZOOM:
                return Response({"error": "Changing gallery view page is not supported for this meeting type"}, status=status.HTTP_400_BAD_REQUEST)
            if not bot.use_zoom_web_adapter():
                return Response({"error": "Changing gallery view page is only supported for Zoom Web SDK"}, status=status.HTTP_400_BAD_REQUEST)
            if bot.recording_view() != RecordingViews.GALLERY_VIEW:
                return Response({"error": "Changing gallery view page is only supported for when recording in gallery view"}, status=status.HTTP_400_BAD_REQUEST)

            # Check if bot is in a state that allows changing the gallery view page
            if not BotEventManager.is_state_that_can_change_gallery_view_page(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot change the gallery view page"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            direction = request.data.get("direction", "next")
            sync_command_to_send = "change_gallery_view_page_next" if direction == "next" else "change_gallery_view_page_previous"

            # Call the utility method on the bot instance to change the gallery view page
            try:
                logging.info(f"Changing gallery view page for bot {bot.object_id} to {direction}")

                send_sync_command(bot, sync_command_to_send)
                return Response(status=status.HTTP_200_OK)
            except Exception as e:
                logging.error(f"Error changing gallery view page for bot {bot.object_id} to {direction}: {str(e)}")
                return Response(
                    {"error": f"Failed to change gallery view page for bot {bot.object_id} to {direction}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class PauseRecordingView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="Pause Recording",
        summary="Pause the bot's recording",
        description="Pauses the recording for the specified bot. This functionality is in beta and only supported for Google Meet and MS Teamsbots.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Recording paused successfully",
            ),
            400: OpenApiResponse(description="Bot is not in a valid state to pause recording"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            # Check if bot is in a state that allows pausing the recording
            if not BotEventManager.is_state_that_can_pause_recording(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot pause recording"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Call the utility method on the bot instance to pause recording
            try:
                logging.info(f"Pausing recording for bot {bot.object_id}")
                send_sync_command(bot, "pause_recording")
                # The best we can do is poll the state of the bot to see if the recording has been paused
                # We'll wait up to one second and if the bot's state has not changed, we'll return an error
                for _ in range(5):
                    time.sleep(0.2)
                    bot.refresh_from_db()
                    if bot.state == BotStates.JOINED_RECORDING_PAUSED:
                        return Response(BotSerializer(bot).data, status=status.HTTP_200_OK)
                    if not BotEventManager.is_state_that_can_pause_recording(bot.state):
                        return Response(
                            {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot pause recording"},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

                logging.error(f"Unable to pause recording for bot {bot.object_id}")
                return Response({"error": "Unable to pause recording"}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logging.error(f"Error pausing recording for bot {bot.object_id}: {str(e)}")
                return Response(
                    {"error": "Failed to pause recording"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class ResumeRecordingView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]

    @extend_schema(
        operation_id="Resume Recording",
        summary="Resume the bot's recording",
        description="Resumes the recording for the specified bot.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Recording resumed successfully",
            ),
            400: OpenApiResponse(description="Bot is not in a valid state to resume recording"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            # Check if bot is in a state that allows resuming the recording
            if not BotEventManager.is_state_that_can_resume_recording(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot resume recording."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Call the utility method on the bot instance to resume recording
            try:
                logging.info(f"Resuming recording for bot {bot.object_id}")
                send_sync_command(bot, "resume_recording")
                # The best we can do is poll the state of the bot to see if the recording has been resumed
                # We'll wait up to one second and if the bot's state has not changed, we'll return an error
                for _ in range(5):
                    time.sleep(0.2)
                    bot.refresh_from_db()
                    if bot.state == BotStates.JOINED_RECORDING:
                        return Response(BotSerializer(bot).data, status=status.HTTP_200_OK)
                    if not BotEventManager.is_state_that_can_resume_recording(bot.state):
                        return Response(
                            {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot resume recording."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

                logging.error(f"Unable to resume recording for bot {bot.object_id}")
                return Response({"error": "Unable to resume recording"}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logging.error(f"Error resuming recording for bot {bot.object_id}: {str(e)}")
                return Response(
                    {"error": "Failed to resume recording"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class ParticipantEventCursorPagination(CursorPagination):
    ordering = "created_at"
    page_size = 25


class ParticipantEventsView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    pagination_class = ParticipantEventCursorPagination
    serializer_class = ParticipantEventSerializer

    @extend_schema(
        operation_id="Get Participant Events",
        summary="Get participant events for a bot",
        description="Returns the participant events (join/leave) for a bot. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=ParticipantEventSerializer(many=True),
                description="List of participant events",
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
            OpenApiParameter(
                name="after",
                type={"type": "string", "format": "ISO 8601 datetime"},
                location=OpenApiParameter.QUERY,
                description="Only return participant events created after this time. Useful when polling for updates.",
                required=False,
                examples=[OpenApiExample("DateTime Example", value="2024-01-18T12:34:56Z")],
            ),
            OpenApiParameter(
                name="before",
                type={"type": "string", "format": "ISO 8601 datetime"},
                location=OpenApiParameter.QUERY,
                description="Only return participant events created before this time.",
                required=False,
                examples=[OpenApiExample("DateTime Example", value="2024-01-18T13:34:56Z")],
            ),
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Cursor for pagination",
                required=False,
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            # Get the bot and verify it belongs to the project
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            # Get optional after and before parameters
            after = request.query_params.get("after")
            before = request.query_params.get("before")

            # Query participant events for this bot. Do not show events for the bot itself
            events_query = ParticipantEvent.objects.filter(participant__bot=bot, participant__is_the_bot=False).select_related("participant")

            # Filter by after if provided
            if after:
                try:
                    after_datetime = parse_datetime(str(after))
                except Exception:
                    after_datetime = None

                if not after_datetime:
                    return Response(
                        {"error": "Invalid after format. Use ISO 8601 format (e.g., 2024-01-18T12:34:56Z)"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                events_query = events_query.filter(created_at__gt=after_datetime)

            # Filter by before if provided
            if before:
                try:
                    before_datetime = parse_datetime(str(before))
                except Exception:
                    before_datetime = None

                if not before_datetime:
                    return Response(
                        {"error": "Invalid before format. Use ISO 8601 format (e.g., 2024-01-18T12:34:56Z)"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                events_query = events_query.filter(created_at__lt=before_datetime)

            # Apply ordering for cursor pagination
            events = events_query.order_by("created_at")

            # Let the pagination class handle the rest
            page = self.paginate_queryset(events)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(events, many=True)
            return Response(serializer.data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class ParticipantCursorPagination(CursorPagination):
    ordering = "created_at"
    page_size = 25


class ParticipantsView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    pagination_class = ParticipantCursorPagination
    serializer_class = ParticipantSerializer

    @extend_schema(
        operation_id="Get Participants",
        summary="Get participants for a bot",
        description="Returns the participants for a bot. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=ParticipantSerializer(many=True),
                description="List of participants",
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
            OpenApiParameter(
                name="cursor",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Cursor for pagination",
                required=False,
            ),
            OpenApiParameter(
                name="is_host",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="Filter participants by whether they are the meeting host",
                required=False,
            ),
            OpenApiParameter(
                name="id",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter participants by participant ID",
                required=False,
                examples=[OpenApiExample("Participant ID Example", value="par_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            # Get the bot and verify it belongs to the project
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            # Query participants for this bot. Do not show the participant for the bot itself
            participants_query = Participant.objects.filter(bot=bot, is_the_bot=False)

            # Optional filters
            is_host_param = request.query_params.get("is_host")
            if is_host_param is not None:
                value = str(is_host_param).strip().lower()
                if value == "true":
                    participants_query = participants_query.filter(is_host=True)
                elif value == "false":
                    participants_query = participants_query.filter(is_host=False)
                else:
                    return Response({"error": "Invalid is_host value. Use true or false."}, status=status.HTTP_400_BAD_REQUEST)

            participant_id = request.query_params.get("id")
            if participant_id:
                participants_query = participants_query.filter(object_id=participant_id)

            # Apply ordering for cursor pagination
            participants = participants_query.order_by("created_at")

            # Let the pagination class handle the rest
            page = self.paginate_queryset(participants)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(participants, many=True)
            return Response(serializer.data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)
