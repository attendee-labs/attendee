from django.urls import path

from . import app_session_api_views, bots_api_views

urlpatterns = [
    path("app_sessions", app_session_api_views.AppSessionCreateView.as_view(), name="app-session-create"),
    path(
        "app_sessions/end",
        app_session_api_views.AppSessionEndView.as_view(),
        name="app-session-end",
    ),
    path(
        "app_sessions/<str:object_id>/media",
        app_session_api_views.AppSessionMediaView.as_view(),
        name="app-session-media",
    ),
    path(
        "app_sessions/<str:object_id>/transcript",
        bots_api_views.TranscriptView.as_view(),
        name="app-session-transcript",
    ),
    path(
        "app_sessions/<str:object_id>/participant_events",
        bots_api_views.ParticipantEventsView.as_view(),
        name="app-session-participant-events",
    ),
]
