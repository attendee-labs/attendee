"""
URL configuration for attendee project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

import os

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from accounts import views


def health_check(request):
    return HttpResponse(status=200)


urlpatterns = [
    path("health/", health_check, name="health-check"),
]

if not os.environ.get("DISABLE_ADMIN"):
    urlpatterns.append(path("admin/", admin.site.urls))

urlpatterns += [
    path("accounts/", include("allauth.urls")),
    path("accounts/", include("allauth.socialaccount.urls")),
    path("external_webhooks/", include("bots.external_webhooks_urls", namespace="external_webhooks")),
    path("bot_sso/", include("bots.bot_sso_urls", namespace="bot_sso")),
    path("", views.home, name="home"),
    path("projects/", include("bots.projects_urls", namespace="projects")),
    path("api/v1/", include("bots.calendars_api_urls")),
    path("api/v1/", include("bots.zoom_oauth_connections_api_urls")),
    path("api/v1/", include("bots.app_session_api_urls")),
    path("api/v1/", include("bots.bots_api_urls")),
]

if settings.DEBUG:
    # API docs routes - only available in development
    urlpatterns += [
        path("schema/", SpectacularAPIView.as_view(), name="schema"),
        path(
            "schema/swagger-ui/",
            SpectacularSwaggerView.as_view(url_name="schema"),
            name="swagger-ui",
        ),
        path(
            "schema/redoc/",
            SpectacularRedocView.as_view(url_name="schema"),
            name="redoc",
        ),
    ]
