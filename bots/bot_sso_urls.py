from django.urls import path

from . import bot_sso_views

urlpatterns = [
    path(
        "google-meet-sign-in",
        bot_sso_views.GoogleMeetSignInView.as_view(),
        name="google-meet-sign-in",
    ),
]
