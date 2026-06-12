import logging

from drf_spectacular.openapi import OpenApiResponse
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from .authentication import ApiKeyAuthentication
from .bot_login_groups_api_utils import create_bot_login_group
from .models import BotLoginGroup
from .serializers import BotLoginGroupSerializer, CreateBotLoginGroupSerializer
from .throttling import ProjectPostThrottle

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

NewlyCreatedBotLoginGroupExample = OpenApiExample(
    "Newly Created Bot Login Group",
    value={
        "id": "blg_abcdef1234567890",
        "platform": "google_meet",
        "name": "My Login Group",
        "created_at": "2025-01-13T10:30:00.123456Z",
        "updated_at": "2025-01-13T10:30:00.123456Z",
    },
    description="Example response when a bot login group is successfully created",
)


class BotLoginGroupCursorPagination(CursorPagination):
    ordering = "-created_at"
    page_size = 25


class BotLoginGroupListCreateView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    pagination_class = BotLoginGroupCursorPagination
    serializer_class = BotLoginGroupSerializer

    @extend_schema(
        operation_id="List Bot Login Groups",
        summary="List bot login groups",
        description="Returns a list of bot login groups for the authenticated project. Results are paginated using cursor pagination.",
        responses={
            200: OpenApiResponse(
                response=BotLoginGroupSerializer(many=True),
                description="List of bot login groups",
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
        tags=["Bot Login Groups"],
    )
    def get(self, request):
        bot_login_groups = BotLoginGroup.objects.filter(project=request.auth.project).order_by("-created_at")

        page = self.paginate_queryset(bot_login_groups)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(bot_login_groups, many=True)
        return Response(serializer.data)

    @extend_schema(
        operation_id="Create Bot Login Group",
        summary="Create a new bot login group",
        description="A bot login group holds a set of bot logins for a single meeting platform.",
        request=CreateBotLoginGroupSerializer,
        responses={
            201: OpenApiResponse(
                response=BotLoginGroupSerializer,
                description="Bot Login Group created successfully",
                examples=[NewlyCreatedBotLoginGroupExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Bot Login Groups"],
    )
    def post(self, request):
        bot_login_group, error = create_bot_login_group(data=request.data, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        return Response(BotLoginGroupSerializer(bot_login_group).data, status=status.HTTP_201_CREATED)


class BotLoginGroupDetailDeleteView(GenericAPIView):
    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    serializer_class = BotLoginGroupSerializer

    @extend_schema(
        operation_id="Get Bot Login Group",
        summary="Get a bot login group",
        description="Gets a bot login group.",
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot Login Group ID",
                examples=[OpenApiExample("Bot Login Group ID Example", value="blg_abcdef1234567890")],
            ),
        ],
        responses={
            200: OpenApiResponse(response=BotLoginGroupSerializer, description="Bot Login Group retrieved successfully"),
            404: OpenApiResponse(description="Bot Login Group not found"),
        },
        tags=["Bot Login Groups"],
    )
    def get(self, request, object_id):
        try:
            bot_login_group = BotLoginGroup.objects.get(object_id=object_id, project=request.auth.project)
            return Response(BotLoginGroupSerializer(bot_login_group).data, status=status.HTTP_200_OK)
        except BotLoginGroup.DoesNotExist:
            return Response({"error": "Bot Login Group not found"}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        operation_id="Delete Bot Login Group",
        summary="Delete a bot login group",
        description="Deletes a bot login group and all of its bot logins.",
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot Login Group ID",
                examples=[OpenApiExample("Bot Login Group ID Example", value="blg_abcdef1234567890")],
            ),
        ],
        responses={
            200: OpenApiResponse(description="Bot Login Group deleted successfully"),
            404: OpenApiResponse(description="Bot Login Group not found"),
        },
        tags=["Bot Login Groups"],
    )
    def delete(self, request, object_id):
        try:
            bot_login_group = BotLoginGroup.objects.get(object_id=object_id, project=request.auth.project)
            bot_login_group.delete()
            return Response(status=status.HTTP_200_OK)
        except BotLoginGroup.DoesNotExist:
            return Response({"error": "Bot Login Group not found"}, status=status.HTTP_404_NOT_FOUND)
