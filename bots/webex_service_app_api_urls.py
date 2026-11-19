from django.urls import path

from . import webex_service_app_api_views

urlpatterns = [
    path("webex_service_app", webex_service_app_api_views.WebexServiceAppView.as_view(), name="webex-service-app"),
    path("webex_service_app/authorize", webex_service_app_api_views.AuthorizeWebexServiceAppView.as_view(), name="webex-service-app-authorize"),
]
