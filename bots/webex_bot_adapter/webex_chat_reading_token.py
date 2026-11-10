"""A Webex guest token that can read in-meeting chat.

The Service App guest token the bot normally uses (webex_guest_token.py) is refused when it tries to subscribe to a
meeting's chat container. The Webex web client reads chat with a different guest token, which it mints by loading the
meeting page and exchanging a short-lived meeting ticket at Webex's guest identity broker using the web client's own
OAuth client id and secret.

This module reproduces that flow server-side. It is only used when WEBEX_CHAT_READING_CLIENT_ID and
WEBEX_CHAT_READING_CLIENT_SECRET are configured; those are the client id and secret used for chat message reading, and
they are read from the environment, never from the source. The per-meeting values (join id, CSRF token, guest cookie,
meeting ticket) are fetched live from the meeting site each time, so nothing meeting-specific is hardcoded.
"""

import json
import logging
import re
import uuid
from urllib.parse import urlparse

import requests

from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
GUEST_IDBROKER_TOKEN_URL = "https://idbroker-guest-r.wbx2.com/idb/oauth2/v1/access_token"
GUEST_TOKEN_GRANT_TYPE = "urn:cisco:oauth:grant-type:guest-token"
GUEST_TOKEN_SCOPE = "webex-guest:meet_join"
# The web client identifies itself this way on preJoin; the meeting site expects a Webex web client user agent.
SPARK_USER_AGENT = "webex-js-sdk/3.12.0 (web) webex-web-client cantina/1.0"


def _site_base_and_path(meeting_url: str) -> tuple[str, str]:
    parsed = urlparse(meeting_url)
    return f"{parsed.scheme}://{parsed.netloc}", parsed.path


def _extract(pattern: str, text: str, what: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise WebexAuthError(f"Could not find {what} on the Webex meeting page")
    return match.group(1)


def create_chat_reading_guest_token(meeting_url: str, client_id: str, client_secret: str, display_name: str) -> str:
    """Mint a guest token that can read in-meeting chat for the meeting at meeting_url.

    Reproduces the Webex web client's guest sign-in: load the meeting page for the join id, CSRF token and guest cookie,
    register the guest cookie, call preJoin for a signed meeting ticket, then exchange that ticket for a guest access
    token at Webex's guest identity broker using client_id and client_secret.
    """
    site_base, meeting_path = _site_base_and_path(meeting_url)
    site_url = urlparse(site_base).netloc
    tracking_id = f"attendee_{uuid.uuid4()}"
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "trackingid": tracking_id})

    # The meeting page carries the per-meeting join id, CSRF token and guest cookie.
    page = session.get(f"{site_base}{meeting_path}", timeout=REQUEST_TIMEOUT_SECONDS)
    if not page.ok:
        raise WebexAuthError(f"Loading the Webex meeting page failed with {page.status_code}")
    join_tx_id = _extract(r'joinTXId=([^&"\']+)', page.text, "the meeting join id")
    csrf_token = _extract(r'"csrfToken"\s*:\s*"([^"]+)"', page.text, "the CSRF token")
    guest_cookie = _extract(r'"guestCookie"\s*:\s*"([^"]+)"', page.text, "the guest cookie")
    sip_url = _extract(r'sip=([^&"\\]+)', page.text, "the meeting SIP address")

    # Register the guest cookie so preJoin recognizes this guest.
    browser_id = uuid.uuid4().hex
    guest_cookie_response = session.post(
        f"{site_base}/wbxmjs/api/v1/meetings/guestCookie?siteurl={site_url}",
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_token},
        data=json.dumps({"guestCookie": guest_cookie, "browserId": browser_id, "machineId": ""}),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not guest_cookie_response.ok:
        raise WebexAuthError(f"Registering the Webex guest cookie failed with {guest_cookie_response.status_code}")

    # preJoin returns a short-lived signed meeting ticket (guestJWT) for this specific meeting.
    prejoin_response = session.post(
        f"{site_base}/wbxappapi/v1/preJoin",
        headers={"Content-Type": "application/json", "cisco-no-http-redirect": "true", "spark-user-agent": SPARK_USER_AGENT, "correlationId": str(uuid.uuid4())},
        data=json.dumps({"joinTXId": join_tx_id, "guestName": display_name, "supportU2CV2": True, "supportHostKey": True, "sipUrl": sip_url, "locale": "en-US"}),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not prejoin_response.ok:
        raise WebexAuthError(f"Webex preJoin failed with {prejoin_response.status_code}")
    guest_jwt = prejoin_response.json().get("guestJWT")
    if not guest_jwt:
        raise WebexAuthError("Webex preJoin did not return a guest ticket")

    # Exchange the meeting ticket for a guest access token at Webex's guest identity broker.
    token_response = session.post(
        GUEST_IDBROKER_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": GUEST_TOKEN_GRANT_TYPE, "scope": GUEST_TOKEN_SCOPE, "webexapp_jwt": guest_jwt},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not token_response.ok:
        raise WebexAuthError(f"Exchanging the Webex guest ticket failed with {token_response.status_code}: {token_response.text[:200]}")
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise WebexAuthError("Webex guest token exchange did not return an access token")
    return access_token
