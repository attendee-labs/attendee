import logging

from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .app_session_api_views import TokenHeaderParameter
from .authentication import ApiKeyAuthentication
from .models import RecorderUpload, RecorderUploadStates, SessionTypes
from .recorder_sessions_api_utils import (
    abort_recorder_session,
    complete_recorder_session,
    create_recorder_session,
    part_upload_urls,
    received_parts,
    touch,
)
from .recorder_sessions_serializers import (
    CompleteRecorderSessionSerializer,
    CreateRecorderSessionSerializer,
    PartUploadUrlsSerializer,
    RecorderSessionSerializer,
)
from .recorder_upload_storage import multipart_part_size_bytes, upload_url_expiry_seconds

logger = logging.getLogger(__name__)

SessionIdParameter = OpenApiParameter(
    name="object_id",
    type=str,
    location=OpenApiParameter.PATH,
    description="Recorder session ID",
    examples=[OpenApiExample("Recorder Session ID Example", value="drec_xxxxxxxxxxx")],
)


def _get_recorder_upload(object_id, project):
    """Resolve a recorder session (by its Bot object_id) scoped to the API key's project."""
    return (
        RecorderUpload.objects.select_related("bot")
        .filter(
            bot__object_id=object_id,
            project=project,
            bot__session_type=SessionTypes.DESKTOP_RECORDING,
        )
        .first()
    )


def _upload_context(recorder_upload, part_numbers):
    return {
        "upload": {
            "part_size_bytes": multipart_part_size_bytes(),
            "url_expiry_seconds": upload_url_expiry_seconds(),
            "part_urls": part_upload_urls(recorder_upload, part_numbers),
        }
    }


class RecorderSessionCreateView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Create Recorder Session",
        summary="Create a desktop recorder session",
        description="Creates a recorder session and initiates a resumable multipart upload. Returns presigned part-upload URLs the SDK PUTs media chunks to, then call the complete endpoint.",
        request=CreateRecorderSessionSerializer,
        responses={
            201: OpenApiResponse(response=RecorderSessionSerializer, description="Recorder session created"),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Recorder Sessions"],
    )
    def post(self, request):
        serializer = CreateRecorderSessionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        recorder_upload, error = create_recorder_session(serializer.validated_data, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        num_parts = serializer.validated_data.get("num_parts", 1)
        context = _upload_context(recorder_upload, list(range(1, num_parts + 1)))
        return Response(RecorderSessionSerializer(recorder_upload, context=context).data, status=status.HTTP_201_CREATED)


class RecorderSessionDetailView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Recorder Session",
        summary="Get a recorder session's status",
        description="Returns the session's upload and recording state, plus the parts already received by storage (for resuming an interrupted upload).",
        responses={
            200: OpenApiResponse(response=RecorderSessionSerializer, description="Recorder session status"),
            404: OpenApiResponse(description="Recorder session not found"),
        },
        parameters=[*TokenHeaderParameter, SessionIdParameter],
        tags=["Recorder Sessions"],
    )
    def get(self, request, object_id):
        recorder_upload = _get_recorder_upload(object_id, request.auth.project)
        if not recorder_upload:
            return Response({"error": "Recorder session not found"}, status=status.HTTP_404_NOT_FOUND)

        context = {}
        # Only query storage for resume info while the upload is still active.
        if not recorder_upload.is_terminal() and recorder_upload.upload_id:
            context["received_parts"] = received_parts(recorder_upload)
        return Response(RecorderSessionSerializer(recorder_upload, context=context).data)


class RecorderSessionPartsView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Recorder Session Part URLs",
        summary="Get more presigned part-upload URLs",
        description="Issues additional presigned multipart part-upload URLs for a long recording.",
        request=PartUploadUrlsSerializer,
        responses={
            200: OpenApiResponse(response=RecorderSessionSerializer, description="Presigned part URLs"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Recorder session not found"),
        },
        parameters=[*TokenHeaderParameter, SessionIdParameter],
        tags=["Recorder Sessions"],
    )
    def post(self, request, object_id):
        recorder_upload = _get_recorder_upload(object_id, request.auth.project)
        if not recorder_upload:
            return Response({"error": "Recorder session not found"}, status=status.HTTP_404_NOT_FOUND)
        if recorder_upload.is_terminal():
            return Response({"error": f"Recorder session is in terminal state '{RecorderUploadStates.state_to_api_code(recorder_upload.state)}'."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = PartUploadUrlsSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Requesting more part URLs is genuine upload progress; keep the reaper from
        # expiring an actively-uploading long recording.
        touch(recorder_upload)

        context = _upload_context(recorder_upload, serializer.validated_data["part_numbers"])
        return Response(RecorderSessionSerializer(recorder_upload, context=context).data)


class RecorderSessionCompleteView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Complete Recorder Session",
        summary="Finalize a recorder session upload",
        description="Completes the multipart upload, attaches the media to the recording, and marks the recording complete. Idempotent.",
        request=CompleteRecorderSessionSerializer,
        responses={
            200: OpenApiResponse(response=RecorderSessionSerializer, description="Recorder session completed"),
            400: OpenApiResponse(description="Invalid input or upload could not be finalized"),
            404: OpenApiResponse(description="Recorder session not found"),
        },
        parameters=[*TokenHeaderParameter, SessionIdParameter],
        tags=["Recorder Sessions"],
    )
    def post(self, request, object_id):
        recorder_upload = _get_recorder_upload(object_id, request.auth.project)
        if not recorder_upload:
            return Response({"error": "Recorder session not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = CompleteRecorderSessionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        recorder_upload, error = complete_recorder_session(recorder_upload, serializer.validated_data.get("parts", []))
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        return Response(RecorderSessionSerializer(recorder_upload).data)


class RecorderSessionAbortView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Abort Recorder Session",
        summary="Abort a recorder session",
        description="Cancels the session, aborts the multipart upload, and releases orphaned storage.",
        request=None,
        responses={
            200: OpenApiResponse(response=RecorderSessionSerializer, description="Recorder session aborted"),
            400: OpenApiResponse(description="Cannot abort a completed session"),
            404: OpenApiResponse(description="Recorder session not found"),
        },
        parameters=[*TokenHeaderParameter, SessionIdParameter],
        tags=["Recorder Sessions"],
    )
    def post(self, request, object_id):
        recorder_upload = _get_recorder_upload(object_id, request.auth.project)
        if not recorder_upload:
            return Response({"error": "Recorder session not found"}, status=status.HTTP_404_NOT_FOUND)

        recorder_upload, error = abort_recorder_session(recorder_upload)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        return Response(RecorderSessionSerializer(recorder_upload).data)
