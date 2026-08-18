from django.urls import path

from . import bots_api_views, recorder_sessions_api_views

urlpatterns = [
    path("recorder_sessions", recorder_sessions_api_views.RecorderSessionCreateView.as_view(), name="recorder-session-create"),
    path("recorder_sessions/<str:object_id>", recorder_sessions_api_views.RecorderSessionDetailView.as_view(), name="recorder-session-detail"),
    path("recorder_sessions/<str:object_id>/parts", recorder_sessions_api_views.RecorderSessionPartsView.as_view(), name="recorder-session-parts"),
    path("recorder_sessions/<str:object_id>/complete", recorder_sessions_api_views.RecorderSessionCompleteView.as_view(), name="recorder-session-complete"),
    path("recorder_sessions/<str:object_id>/abort", recorder_sessions_api_views.RecorderSessionAbortView.as_view(), name="recorder-session-abort"),
    # Download reuses the existing bot recording view (resolves by object_id + project).
    path("recorder_sessions/<str:object_id>/recording", bots_api_views.RecordingView.as_view(), name="recorder-session-recording"),
]
