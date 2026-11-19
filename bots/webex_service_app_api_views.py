import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.openapi import OpenApiResponse
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from .authentication import ApiKeyAuthentication
from .models import WebexServiceApp
from .serializers import AuthorizeWebexServiceAppSerializer, SetWebexServiceAppSerializer, WebexServiceAppSerializer
from .throttling import ProjectPostThrottle
from .webex_bot_adapter.webex_guest_token import WebexAuthError, exchange_integration_authorization_code, issue_service_app_tokens

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

WebexServiceAppExample = OpenApiExample(
    "Webex Service App",
    value={
        "id": "wsa_abcdef1234567890",
        "client_id": "C0123456789abcdef",
        "service_app_id": "Y2lzY29zcGFyazovL3VzL0FQUExJQ0FUSU9OL2FiY2RlZg",
        "org_id": "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9hYmNkZWY",
        "is_authorized": True,
        "tokens_refreshed_at": "2026-11-18T10:30:00.123456Z",
        "created_at": "2026-11-18T10:29:00.123456Z",
        "updated_at": "2026-11-18T10:30:00.123456Z",
    },
    description="A Webex Service App that has been authorized",
)

NOT_FOUND_RESPONSE = {"error": "Webex Service App not found"}


def webex_service_app_for_project(project):
    return WebexServiceApp.objects.filter(project=project).first()


class WebexServiceAppView(GenericAPIView):
    """The project's Webex Service App, which Webex bots join meetings through. A project has at most one."""

    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    serializer_class = WebexServiceAppSerializer

    @extend_schema(
        operation_id="Get Webex Service App",
        summary="Get the project's Webex Service App",
        description="Returns the project's Webex Service App and whether it is authorized. Its client secret and tokens are never returned.",
        parameters=TokenHeaderParameter,
        responses={
            200: OpenApiResponse(response=WebexServiceAppSerializer, description="The Webex Service App", examples=[WebexServiceAppExample]),
            404: OpenApiResponse(description="The project has no Webex Service App"),
        },
        tags=["Webex Service App"],
    )
    def get(self, request):
        webex_service_app = webex_service_app_for_project(request.auth.project)
        if not webex_service_app:
            return Response(NOT_FOUND_RESPONSE, status=status.HTTP_404_NOT_FOUND)
        return Response(WebexServiceAppSerializer(webex_service_app).data)

    @extend_schema(
        operation_id="Set Webex Service App",
        summary="Set the project's Webex Service App",
        description="Saves the project's Webex Service App, replacing any existing one. A replaced Service App loses its authorization, so authorize the new one before launching Webex bots.",
        request=SetWebexServiceAppSerializer,
        parameters=TokenHeaderParameter,
        responses={
            200: OpenApiResponse(response=WebexServiceAppSerializer, description="The saved Webex Service App, not authorized yet"),
            400: OpenApiResponse(description="Invalid input"),
        },
        tags=["Webex Service App"],
    )
    def put(self, request):
        serializer = SetWebexServiceAppSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        fields = serializer.validated_data

        with transaction.atomic():
            WebexServiceApp.objects.filter(project=request.auth.project).delete()
            webex_service_app = WebexServiceApp(project=request.auth.project, client_id=fields["client_id"], service_app_id=fields["service_app_id"], org_id=fields["org_id"])
            webex_service_app.set_credentials({"client_secret": fields["client_secret"]})

        return Response(WebexServiceAppSerializer(webex_service_app).data)

    @extend_schema(
        operation_id="Delete Webex Service App",
        summary="Delete the project's Webex Service App",
        description="Deletes the project's Webex Service App and its tokens. The project can't launch Webex bots until a new one is set and authorized.",
        parameters=TokenHeaderParameter,
        responses={
            200: OpenApiResponse(description="Webex Service App deleted"),
            404: OpenApiResponse(description="The project has no Webex Service App"),
        },
        tags=["Webex Service App"],
    )
    def delete(self, request):
        deleted_count, _ = WebexServiceApp.objects.filter(project=request.auth.project).delete()
        if not deleted_count:
            return Response(NOT_FOUND_RESPONSE, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_200_OK)


class AuthorizeWebexServiceAppView(GenericAPIView):
    """Issues the project's Webex Service App its tokens, as the 'Authorize with Webex' button in the UI does."""

    authentication_classes = [ApiKeyAuthentication]
    throttle_classes = [ProjectPostThrottle]
    serializer_class = WebexServiceAppSerializer

    @extend_schema(
        operation_id="Authorize Webex Service App",
        summary="Authorize the project's Webex Service App",
        description=("Issues the Service App's tokens with a Webex token of the user who created it, so the project can launch Webex bots. Pass either the authorization_code (and redirect_uri) that Webex sent to your own redirect URI after that user signed in to the Webex Integration with the spark:applications_token scope, or an access_token of that user with that scope. Attendee keeps the Service App's tokens fresh from then on."),
        request=AuthorizeWebexServiceAppSerializer,
        parameters=TokenHeaderParameter,
        responses={
            200: OpenApiResponse(response=WebexServiceAppSerializer, description="The authorized Webex Service App", examples=[WebexServiceAppExample]),
            400: OpenApiResponse(description="Invalid input, or Webex did not issue the tokens"),
            404: OpenApiResponse(description="The project has no Webex Service App"),
        },
        tags=["Webex Service App"],
    )
    def post(self, request):
        webex_service_app = webex_service_app_for_project(request.auth.project)
        if not webex_service_app:
            return Response(NOT_FOUND_RESPONSE, status=status.HTTP_404_NOT_FOUND)

        serializer = AuthorizeWebexServiceAppSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        if data.get("authorization_code") and not (settings.WEBEX_INTEGRATION_CLIENT_ID and settings.WEBEX_INTEGRATION_CLIENT_SECRET):
            return Response({"error": "WEBEX_INTEGRATION_CLIENT_ID and WEBEX_INTEGRATION_CLIENT_SECRET are not set, so an authorization code can't be exchanged. Pass an access_token instead."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if data.get("authorization_code"):
                bearer_token = exchange_integration_authorization_code(settings.WEBEX_INTEGRATION_CLIENT_ID, settings.WEBEX_INTEGRATION_CLIENT_SECRET, data["authorization_code"], data["redirect_uri"])["access_token"]
            else:
                bearer_token = data["access_token"]
            tokens = issue_service_app_tokens(webex_service_app.service_app_id, webex_service_app.client_id, webex_service_app.client_secret, webex_service_app.org_id, bearer_token)
        except WebexAuthError as e:
            logger.warning(f"Webex Service App {webex_service_app.object_id} could not be authorized through the API: {e}")
            return Response({"error": f"Webex did not issue the Service App's tokens: {e}"}, status=status.HTTP_400_BAD_REQUEST)

        webex_service_app.tokens_refreshed_at = timezone.now()
        webex_service_app.set_credentials({**webex_service_app.get_credentials(), "refresh_token": tokens["refresh_token"]})
        logger.info(f"Webex Service App {webex_service_app.object_id} was authorized through the API")
        return Response(WebexServiceAppSerializer(webex_service_app).data)
