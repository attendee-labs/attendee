from django.urls import path

from . import zoom_oauth_connections_api_views

urlpatterns = [
    path("zoom_oauth_connections", zoom_oauth_connections_api_views.ZoomOAuthConnectionListCreateView.as_view(), name="zoom-oauth-connection-list-create"),
    path("zoom_oauth_connections/<str:object_id>/access_token", zoom_oauth_connections_api_views.ZoomOAuthConnectionAccessTokenView.as_view(), name="zoom-oauth-connection-access-token"),
    path("zoom_oauth_connections/<str:object_id>/zak_token", zoom_oauth_connections_api_views.ZoomOAuthConnectionZakTokenView.as_view(), name="zoom-oauth-connection-zak-token"),
    path("zoom_oauth_connections/<str:object_id>", zoom_oauth_connections_api_views.ZoomOAuthConnectionDetailPatchDeleteView.as_view(), name="zoom-oauth-connection-detail-patch-delete"),
]
