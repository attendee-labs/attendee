from django.urls import path

from . import bot_sso_views

app_name = "bot_sso"

urlpatterns = [
    # SAML endpoints
    path(
        "google_meet_sign_in",
        bot_sso_views.GoogleMeetSignInView.as_view(),
        name="google_meet_sign_in",
    ),
    path(
        "google_meet_sign_out",
        bot_sso_views.GoogleMeetSignOutView.as_view(),
        name="google_meet_sign_out",
    ),
    # Shared endpoint (SAML + OIDC)
    path(
        "google_meet_set_cookie",
        bot_sso_views.GoogleMeetSetCookieView.as_view(),
        name="google_meet_set_cookie",
    ),
    # OIDC endpoints
    path(
        ".well-known/openid-configuration",
        bot_sso_views.OIDCDiscoveryView.as_view(),
        name="oidc_discovery",
    ),
    path(
        "authorize",
        bot_sso_views.OIDCAuthorizeView.as_view(),
        name="oidc_authorize",
    ),
    path(
        "token",
        bot_sso_views.OIDCTokenView.as_view(),
        name="oidc_token",
    ),
    path(
        "jwks",
        bot_sso_views.OIDCJWKSView.as_view(),
        name="oidc_jwks",
    ),
    path(
        "userinfo",
        bot_sso_views.OIDCUserInfoView.as_view(),
        name="oidc_userinfo",
    ),
]
