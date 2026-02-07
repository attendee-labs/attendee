import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from bots.bot_sso_utils import _build_sign_in_saml_response, _html_auto_post_form, get_bot_login_for_google_meet_sign_in_session

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class GoogleMeetSetCookieView(View):
    """
    GET endpoint that sets a cookie for the Google Meet SSO flow based on the session id.
    The cookie is used to identify the session when we receive a SAML AuthnRequest.
    """

    def get(self, request):
        # There should be a query parameter called "session_id"
        session_id = request.GET.get("session_id")
        if not session_id:
            logger.warning("GoogleMeetSetCookieView could not set cookie: session_id is missing")
            return HttpResponseBadRequest("Could not set cookie")

        # Check in redis store to confirm that a key with the id "google_meet_sign_in_session:<session_id>" exists
        if not get_bot_login_for_google_meet_sign_in_session(session_id):
            logger.warning("GoogleMeetSetCookieView could not set cookie: no bot login found for session_id")
            return HttpResponseBadRequest("Could not set cookie")

        # Set a cookie with the session_id
        response = HttpResponse("Google Meet Set Cookie")
        response.set_cookie(
            "google_meet_sign_in_session_id",
            session_id,
            secure=True,
            httponly=True,
            samesite="Lax",
        )
        logger.info("GoogleMeetSetCookieView successfully set cookie")
        return response


@method_decorator(csrf_exempt, name="dispatch")
class GoogleMeetSignInView(View):
    """
    GET endpoint that receives a SAML AuthnRequest via HTTP-Redirect binding and
    returns an auto-submitting HTML form that POSTs a signed SAMLResponse to the ACS.
    """

    def get(self, request):
        # Get the session_id from the cookie
        session_id = request.COOKIES.get("google_meet_sign_in_session_id")
        if not session_id:
            logger.warning("GoogleMeetSignInView could not sign in: session_id is missing")
            return HttpResponseBadRequest("Could not sign in")

        # Get the google meet bot login to use from the session id
        google_meet_bot_login = get_bot_login_for_google_meet_sign_in_session(session_id)
        if not google_meet_bot_login:
            logger.warning("GoogleMeetSignInView could not sign in: no bot login found for session_id")
            return HttpResponseBadRequest("Could not sign in")

        saml_request_b64 = request.GET.get("SAMLRequest")
        relay_state = request.GET.get("RelayState")

        if not saml_request_b64:
            logger.warning("GoogleMeetSignInView could not sign in: SAMLRequest is missing")
            return HttpResponseBadRequest("Missing SAMLRequest")

        # Create and sign the SAMLResponse
        try:
            saml_response_b64, acs_url = _build_sign_in_saml_response(
                saml_request_b64=saml_request_b64,
                email_to_sign_in=google_meet_bot_login.email,
                cert=google_meet_bot_login.cert,
                private_key=google_meet_bot_login.private_key,
            )
        except Exception as e:
            logger.exception(f"Failed to create SAMLResponse: {e}")
            return HttpResponseBadRequest("Failed to create SAMLResponse. Private Key or Cert may be invalid.")

        # 6) Return auto-posting HTML to the ACS
        html = _html_auto_post_form(acs_url, saml_response_b64, relay_state)
        return HttpResponse(html, content_type="text/html")


@method_decorator(csrf_exempt, name="dispatch")
class GoogleMeetSignOutView(View):
    """
    GET endpoint that receives a SAML LogoutRequest via HTTP-Redirect binding
    """

    def get(self, request):
        logger.info("GoogleMeetSignOutView GET request received")
        # For now, we'll do nothing here. In the future may be useful keeping track of active sessions more rigorously.
        return HttpResponse("Signed Out Successfully")
