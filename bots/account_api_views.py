from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from .app_session_api_views import TokenHeaderParameter
from .authentication import ApiKeyAuthentication
from .recorder_sessions_serializers import AccountSerializer


class AccountView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Account",
        summary="Get the account for the API key",
        description="Validates the API key and returns the project, organization, and key it belongs to. Intended for a client (e.g. desktop SDK) to confirm its credentials and display the connected project.",
        responses={
            200: OpenApiResponse(response=AccountSerializer, description="Account info for the API key"),
            401: OpenApiResponse(description="Invalid or missing API key"),
        },
        parameters=TokenHeaderParameter,
        tags=["Account"],
    )
    def get(self, request):
        data = {"project": request.auth.project, "api_key": request.auth}
        return Response(AccountSerializer(data).data)
