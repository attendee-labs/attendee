"""Webex guest tokens for the bot.

The bot joins Webex meetings as a guest. Guest tokens are minted by a Webex Service App that has the
guest-issuer:read and guest-issuer:write scopes and has been authorized by a full admin of the Webex organization.

The Service App's first tokens are issued with the token of a Webex Integration that has the spark:applications_token
scope, which the creator of the Service App grants by signing in to Webex once.
"""

from urllib.parse import urlencode

import requests

WEBEX_API_URL = "https://webexapis.com/v1"
REQUEST_TIMEOUT_SECONDS = 30
INTEGRATION_SCOPE = "spark:applications_token"


class WebexAuthError(Exception):
    pass


def _post(path: str, **request_kwargs) -> dict:
    response = requests.post(f"{WEBEX_API_URL}{path}", timeout=REQUEST_TIMEOUT_SECONDS, **request_kwargs)
    if not response.ok:
        raise WebexAuthError(f"POST {path} failed with {response.status_code}: {response.text}")
    return response.json()


def integration_authorize_url(integration_client_id: str, redirect_uri: str, state: str) -> str:
    """The Webex sign-in page where the user grants the Integration the spark:applications_token scope."""
    return f"{WEBEX_API_URL}/authorize?" + urlencode({"client_id": integration_client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": INTEGRATION_SCOPE, "state": state})


def exchange_integration_authorization_code(integration_client_id: str, integration_client_secret: str, code: str, redirect_uri: str) -> dict:
    return _post(
        "/access_token",
        data={"grant_type": "authorization_code", "client_id": integration_client_id, "client_secret": integration_client_secret, "code": code, "redirect_uri": redirect_uri},
    )


def issue_service_app_tokens(service_app_id: str, client_id: str, client_secret: str, target_org_id: str, bearer_token: str) -> dict:
    """Issue the first access and refresh tokens of an authorized Service App.

    bearer_token needs the spark:applications_token scope, which an Integration's token or a personal access token has.
    """
    return _post(
        f"/applications/{service_app_id}/token",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={"clientId": client_id, "clientSecret": client_secret, "targetOrgId": target_org_id},
    )


def refresh_service_app_tokens(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return _post(
        "/access_token",
        data={"grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token},
    )


def create_guest_token(service_app_access_token: str, subject: str, display_name: str) -> str:
    """Mint an access token for a guest identified by subject. It is valid for roughly 18 hours."""
    response = _post(
        "/guests/token",
        headers={"Authorization": f"Bearer {service_app_access_token}"},
        json={"subject": subject, "displayName": display_name},
    )
    return response["accessToken"]


def create_guest_token_from_refresh_token(client_id: str, client_secret: str, refresh_token: str, subject: str, display_name: str) -> str:
    access_token = refresh_service_app_tokens(client_id, client_secret, refresh_token)["access_token"]
    return create_guest_token(access_token, subject, display_name)
