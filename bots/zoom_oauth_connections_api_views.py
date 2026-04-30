import logging

from django.db import transaction
from drf_spectacular.openapi import OpenApiResponse
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from .authentication import ApiKeyAuthentication
from .models import ZoomOAuthConnection, ZoomOAuthConnectionStates
from .serializers import CreateZoomOAuthConnectionSerializer, CreateZoomOAuthConnectionZakTokenSerializer, ZoomOAuthConnectionSerializer
from .tasks.sync_zoom_oauth_connection_task import enqueue_sync_zoom_oauth_connection_task
from .throttling import ProjectPostThrottle
from .zoom_oauth_connections_api_utils import create_zoom_oauth_connection
from .zoom_oauth_connections_utils import ZoomAPIAuthenticationError, ZoomAPIError, _handle_zoom_api_authentication_error, get_zak_token_via_zoom_oauth_connection

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

NewlyCreatedZoomOAuthConnectionExample = OpenApiExample(
    "Newly Created Zoom OAuth Connection",
    value={
        "id": "zoc_abcdef1234567890",
        "zoom_oauth_app": "zoa_abcdef1234567890",
        "state": "connected",
        "metadata": {"tenant_id": "1234567890"},
        "user_id": "user_abcdef1234567890",
        "account_id": "account_abcdef1234567890",
        "connection_failure_data": None,
        "created_at": "2025-01-13T10:30:00.123456Z",
        "updated_at": "2025-01-13T10:30:00.123456Z",
    },
    description="Example response when a zoom oauth connection is successfully created",
)


class ZoomOAuthConnectionCursorPagination(CursorPagination):
    ordering = "-created_at"
    page_size = 25


class ZoomOAuthConnectionListCreateView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    pagination_class = ZoomOAuthConnectionCursorPagination
    serializer_class = ZoomOAuthConnectionSerializer

    @extend_schema(
        operation_id="List Zoom OAuth Connections",
        summary="List zoom oauth connections",
        description="Returns a list of zoom oauth connections for the authenticated project. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=ZoomOAuthConnectionSerializer(many=True),
                description="List of zoom oauth connections",
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
        ],
        tags=["Zoom OAuth Connections"],
    )
    def get(self, request):
        zoom_oauth_connections = ZoomOAuthConnection.objects.filter(zoom_oauth_app__project=request.auth.project)

        zoom_oauth_connections = zoom_oauth_connections.order_by("-created_at")

        # Let the pagination class handle the rest
        page = self.paginate_queryset(zoom_oauth_connections)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(zoom_oauth_connections, many=True)
        return Response(serializer.data)

    @extend_schema(
        operation_id="Create Zoom OAuth Connection",
        summary="Create a new zoom oauth connection",
        description="After being created, the zoom oauth connection will be used to generate tokens for the user.",
        request=CreateZoomOAuthConnectionSerializer,
        responses={
            201: OpenApiResponse(
                response=ZoomOAuthConnectionSerializer,
                description="Zoom OAuth Connection created successfully",
                examples=[NewlyCreatedZoomOAuthConnectionExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Zoom OAuth Connections"],
    )
    def post(self, request):
        zoom_oauth_connection, error = create_zoom_oauth_connection(data=request.data, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        # Immediately sync the zoom oauth connection
        enqueue_sync_zoom_oauth_connection_task(zoom_oauth_connection)

        return Response(ZoomOAuthConnectionSerializer(zoom_oauth_connection).data, status=status.HTTP_201_CREATED)


class ZoomOAuthConnectionDetailPatchDeleteView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    serializer_class = ZoomOAuthConnectionSerializer

    @extend_schema(
        operation_id="Delete Zoom OAuth Connection",
        summary="Delete a zoom oauth connection",
        description="Deletes a zoom oauth connection.",
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Zoom OAuth Connection ID",
                examples=[OpenApiExample("Zoom OAuth Connection ID Example", value="zoc_abcdef1234567890")],
            ),
        ],
        responses={
            200: OpenApiResponse(description="Zoom OAuth Connection deleted successfully"),
            404: OpenApiResponse(description="Zoom OAuth Connection not found"),
        },
        tags=["Zoom OAuth Connections"],
    )
    def delete(self, request, object_id):
        try:
            zoom_oauth_connection = ZoomOAuthConnection.objects.get(object_id=object_id, zoom_oauth_app__project=request.auth.project)
            zoom_oauth_connection.delete()
            return Response(status=status.HTTP_200_OK)
        except ZoomOAuthConnection.DoesNotExist:
            return Response({"error": "Zoom OAuth Connection not found"}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        operation_id="Get Zoom OAuth Connection",
        summary="Get a zoom oauth connection",
        description="Gets a zoom oauth connection.",
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Zoom OAuth Connection ID",
                examples=[OpenApiExample("Zoom OAuth Connection ID Example", value="zoc_abcdef1234567890")],
            ),
        ],
        responses={
            200: OpenApiResponse(response=ZoomOAuthConnectionSerializer, description="Zoom OAuth Connection retrieved successfully"),
            404: OpenApiResponse(description="Zoom OAuth Connection not found"),
        },
        tags=["Zoom OAuth Connections"],
    )
    def get(self, request, object_id):
        try:
            zoom_oauth_connection = ZoomOAuthConnection.objects.get(object_id=object_id, zoom_oauth_app__project=request.auth.project)
            return Response(ZoomOAuthConnectionSerializer(zoom_oauth_connection).data, status=status.HTTP_200_OK)
        except ZoomOAuthConnection.DoesNotExist:
            return Response({"error": "Zoom OAuth Connection not found"}, status=status.HTTP_404_NOT_FOUND)


class ZoomOAuthConnectionZakTokenView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    serializer_class = CreateZoomOAuthConnectionZakTokenSerializer

    @extend_schema(
        operation_id="Create Zoom OAuth Connection ZAK Token",
        summary="Create a Zoom ZAK token",
        description="Generates a short-lived Zoom Meeting SDK ZAK token for a connected Zoom OAuth connection in the authenticated project.",
        request=CreateZoomOAuthConnectionZakTokenSerializer,
        responses={
            200: OpenApiResponse(description="Zoom ZAK token generated successfully"),
            400: OpenApiResponse(description="Invalid input or Zoom OAuth connection state"),
            404: OpenApiResponse(description="Zoom OAuth Connection not found"),
            502: OpenApiResponse(description="Zoom token generation failed"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Zoom OAuth Connection ID",
                examples=[OpenApiExample("Zoom OAuth Connection ID Example", value="zoc_abcdef1234567890")],
            ),
        ],
        tags=["Zoom OAuth Connections"],
    )
    def post(self, request, object_id):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        zoom_oauth_connection = None
        try:
            with transaction.atomic():
                zoom_oauth_connection = ZoomOAuthConnection.objects.select_for_update().get(object_id=object_id, zoom_oauth_app__project=request.auth.project)

                if zoom_oauth_connection.state != ZoomOAuthConnectionStates.CONNECTED:
                    return Response({"error": "Zoom OAuth Connection is not connected"}, status=status.HTTP_400_BAD_REQUEST)

                zak_token = get_zak_token_via_zoom_oauth_connection(zoom_oauth_connection)

            if not zak_token:
                return Response({"error": "Zoom did not return a ZAK token"}, status=status.HTTP_502_BAD_GATEWAY)

            return Response({"zak_token": zak_token}, status=status.HTTP_200_OK)

        except ZoomOAuthConnection.DoesNotExist:
            return Response({"error": "Zoom OAuth Connection not found"}, status=status.HTTP_404_NOT_FOUND)
        except ZoomAPIAuthenticationError as e:
            if zoom_oauth_connection:
                _handle_zoom_api_authentication_error(zoom_oauth_connection, e)
            return Response({"error": "Zoom OAuth Connection authentication failed"}, status=status.HTTP_400_BAD_REQUEST)
        except ZoomAPIError:
            logger.exception("Failed to generate Zoom ZAK token for zoom oauth connection %s", object_id)
            return Response({"error": "Failed to generate Zoom ZAK token"}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception:
            logger.exception("Unexpected error generating Zoom ZAK token for zoom oauth connection %s", object_id)
            return Response({"error": "Failed to generate Zoom ZAK token"}, status=status.HTTP_502_BAD_GATEWAY)
