from django.urls import path

from . import zoom_oauth_connections_api_views

urlpatterns = [
    path("zoom_oauth_connections", zoom_oauth_connections_api_views.ZoomOAuthConnectionListCreateView.as_view(), name="zoom-oauth-connection-list-create"),
    path("zoom_oauth_connections/<str:object_id>/zak_token", zoom_oauth_connections_api_views.ZoomOAuthConnectionZakTokenView.as_view(), name="zoom-oauth-connection-zak-token"),
    path("zoom_oauth_connections/<str:object_id>/onbehalf_token", zoom_oauth_connections_api_views.ZoomOAuthConnectionZakTokenView.as_view(), name="zoom-oauth-connection-onbehalf-token"),
    path("zoom_oauth_connections/<str:object_id>", zoom_oauth_connections_api_views.ZoomOAuthConnectionDetailPatchDeleteView.as_view(), name="zoom-oauth-connection-detail-patch-delete"),
]
