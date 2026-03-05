import base64
import hmac
import html
import json
import logging
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zlib
from datetime import timedelta
from urllib.parse import urlencode

import jwt
import redis
from django.conf import settings
from django.urls import reverse
from saml2 import BINDING_HTTP_POST

# pysaml2
from saml2.config import IdPConfig
from saml2.saml import NAMEID_FORMAT_EMAILADDRESS, NameID
from saml2.server import Server

from bots.bots_api_utils import build_site_url
from bots.models import Bot, GoogleMeetBotLogin
from bots.oidc_keys import get_jwks, get_private_key

logger = logging.getLogger(__name__)


def get_google_meet_set_cookie_url(session_id):
    base_url = build_site_url(reverse("bot_sso:google_meet_set_cookie"))
    query_params = urlencode({"session_id": session_id})
    google_meet_set_cookie_url = f"{base_url}?{query_params}"
    return google_meet_set_cookie_url


def create_google_meet_sign_in_session(bot: Bot, google_meet_bot_login: GoogleMeetBotLogin):
    session_id = str(uuid.uuid4())
    redis_key = f"google_meet_sign_in_session:{session_id}"
    redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    # Save for 30 minutes
    session_data = {
        "bot_object_id": str(bot.object_id),
        "google_meet_bot_login_object_id": str(google_meet_bot_login.object_id),
        "login_email": google_meet_bot_login.email,
        "sso_mode": google_meet_bot_login.sso_mode,
    }
    redis_client.setex(redis_key, 60 * 30, json.dumps(session_data))
    return session_id


def get_bot_login_for_google_meet_sign_in_session(session_id):
    redis_key = f"google_meet_sign_in_session:{session_id}"
    redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    session_data_raw = redis_client.get(redis_key)
    if not session_data_raw:
        logger.info(f"No session data found for google_meet_sign_in_session: {session_id}")
        return None

    try:
        session_data = json.loads(session_data_raw)
    except Exception as e:
        logger.warning(f"Error loading session data for google_meet_sign_in_session: {session_id}. Data: {session_data_raw}. Error: {e}")
        return None

    bot_object_id = session_data.get("bot_object_id")
    google_meet_bot_login_object_id = session_data.get("google_meet_bot_login_object_id")

    bot = Bot.objects.filter(object_id=bot_object_id).first()
    google_meet_bot_login = GoogleMeetBotLogin.objects.filter(object_id=google_meet_bot_login_object_id, group__project=bot.project).first()
    if not google_meet_bot_login:
        logger.info(f"No google_meet_bot_login found for google_meet_sign_in_session: {session_id}. Data: {session_data}")
        return None

    if not bot:
        logger.info(f"No bot found for google_meet_sign_in_session: {session_id}. Data: {session_data}")
        return None

    return google_meet_bot_login


IDP_ENTITY_ID = "https://idp.attendee.local"  # Your IdP entityID (can be any stable URL you control)
IDP_SSO_URL = "https://idp.attendee.local/sso"  # Dummy SSO endpoint to satisfy pysaml2 config
XMLSEC_BINARY = "/usr/bin/xmlsec1"  # adjust if different in your environment

# XML namespaces for parsing the AuthnRequest
NSP = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}


def _inflate_redirect_binding(b64: str) -> bytes:
    """Base64 decode + raw DEFLATE inflate (HTTP-Redirect binding)."""
    raw = base64.b64decode(b64)
    return zlib.decompress(raw, -15)  # raw DEFLATE stream (wbits=-15)


def _parse_authn_request(xml_bytes: bytes):
    """
    Extract from AuthnRequest:
      - request_id
      - issuer (SP entityID)
      - acs_url (AssertionConsumerServiceURL)
      - protocol_binding (optional)
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f"Unable to parse AuthnRequest XML: {e}")

    if root.tag != f"{{{NSP['samlp']}}}AuthnRequest":
        raise ValueError("Not a SAML 2.0 AuthnRequest")

    request_id = root.get("ID")
    acs_url = root.get("AssertionConsumerServiceURL")
    protocol_binding = root.get("ProtocolBinding")

    issuer_el = root.find("saml:Issuer", NSP)
    issuer = issuer_el.text.strip() if issuer_el is not None and issuer_el.text else None

    return {
        "request_id": request_id,
        "issuer": issuer,
        "acs_url": acs_url,
        "protocol_binding": protocol_binding,
    }


SP_MD_TEMPLATE = """<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"
    entityID="{sp_entity_id}">
  <SPSSODescriptor
      protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol"
      AuthnRequestsSigned="false"
      WantAssertionsSigned="true">
    <NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</NameIDFormat>
    <AssertionConsumerService
        index="0"
        isDefault="true"
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        Location="{acs_url}" />
  </SPSSODescriptor>
</EntityDescriptor>
"""


def _build_idp_server(sp_entity_id: str, acs_url: str, cert_file: str, key_file: str) -> Server:
    """
    Construct a minimal pysaml2 IdP Server instance, injecting the SP's metadata inline
    so pysaml2 can resolve the SP entry (avoids KeyError lookups).
    """
    sp_md_xml = SP_MD_TEMPLATE.format(sp_entity_id=sp_entity_id, acs_url=acs_url)

    conf = {
        "entityid": IDP_ENTITY_ID,
        "xmlsec_binary": XMLSEC_BINARY,
        "key_file": key_file,
        "cert_file": cert_file,
        "service": {
            "idp": {
                "endpoints": {
                    "single_sign_on_service": [
                        (IDP_SSO_URL, "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"),
                        (IDP_SSO_URL, "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"),
                    ]
                }
            }
        },
        "security": {
            "want_response_signed": True,
            "want_assertions_signed": True,
            "want_assertions_encrypted": False,
            "signature_algorithm": "rsa-sha256",
            "digest_algorithm": "sha256",
        },
        "metadata": {"inline": [sp_md_xml]},
        "debug": True,
    }
    return Server(config=IdPConfig().load(conf))


def _html_auto_post_form(action_url: str, saml_response_b64: str, relay_state: str | None) -> str:
    """Return a minimal HTML page that auto-POSTs SAMLResponse (+ RelayState if present) to the ACS."""
    rs_input = f'<input type="hidden" name="RelayState" value="{html.escape(str(relay_state), quote=True)}"/>' if relay_state is not None else ""
    return f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8"/>
    <title>SAML Post</title>
  </head>
  <body onload="document.forms[0].submit()">
    <form method="post" action="{action_url}">
      <input type="hidden" name="SAMLResponse" value="{saml_response_b64}"/>
      {rs_input}
      <noscript>
        <p>JavaScript is disabled. Click the button below to continue.</p>
        <button type="submit">Continue</button>
      </noscript>
    </form>
  </body>
</html>"""


def _build_sign_in_saml_response(saml_request_b64: str, email_to_sign_in: str, cert: str, private_key: str) -> str:
    # 1) Inflate + parse the AuthnRequest
    try:
        xml_bytes = _inflate_redirect_binding(saml_request_b64)
        authn = _parse_authn_request(xml_bytes)
    except Exception as e:
        raise ValueError(f"Failed to decode/parse SAMLRequest: {e}")

    acs_url = authn.get("acs_url")
    sp_entity_id = authn.get("issuer")
    in_response_to = authn.get("request_id")

    if not acs_url:
        raise ValueError("AuthnRequest missing AssertionConsumerServiceURL")
    if not sp_entity_id:
        raise ValueError("AuthnRequest missing Issuer")
    if not in_response_to:
        raise ValueError("AuthnRequest missing ID")

    # 2) Build IdP server with inline SP metadata.
    # Write the cert and private key to temporary files, which are deleted after the function completes.

    with tempfile.NamedTemporaryFile("w+", delete=True, encoding="utf-8") as cert_file, tempfile.NamedTemporaryFile("w+", delete=True, encoding="utf-8") as key_file:
        cert_file.write(cert)
        cert_file.flush()
        key_file.write(private_key)
        key_file.flush()

        try:
            idp = _build_idp_server(sp_entity_id, acs_url, cert_file.name, key_file.name)
        except Exception as e:
            raise ValueError(f"Failed to build IdP server: {e}")

        # 3) Build a NameID and (optionally) attributes for the subject
        # Many SPs (incl. Google) are fine with just NameID. Attributes are optional.
        name_id_obj = NameID(format=NAMEID_FORMAT_EMAILADDRESS, text=email_to_sign_in)
        identity = {
            "mail": [email_to_sign_in],
            "email": [email_to_sign_in],
            "uid": [email_to_sign_in],
        }

        saml_resp = idp.create_authn_response(
            identity=identity,
            in_response_to=in_response_to,
            destination=acs_url,
            sp_entity_id=sp_entity_id,
            name_id=name_id_obj,
            name_id_policy={
                "format": NAMEID_FORMAT_EMAILADDRESS,
                "allow_create": "true",
            },
            authn={
                "class_ref": "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport",
                "authn_auth": IDP_ENTITY_ID,
            },
            sign_assertion=True,
            sign_response=True,
            assertion_ttl=int(timedelta(minutes=5).total_seconds()),
            binding=BINDING_HTTP_POST,
            audience_restriction=[sp_entity_id],
        )

        resp_xml = saml_resp
        saml_response_b64 = base64.b64encode(resp_xml.encode("utf-8")).decode("ascii")

        return saml_response_b64, acs_url


# ---------------------------------------------------------------------------
# OIDC helpers
# ---------------------------------------------------------------------------


def get_issuer_url():
    """Return the OIDC issuer URL (base path for discovery)."""
    return f"https://{settings.SITE_DOMAIN}/bot_sso"


def create_auth_code(email, client_id, redirect_uri, nonce=None, code_challenge=None, code_challenge_method=None):
    """Create a single-use authorization code stored in Redis."""
    code = str(uuid.uuid4())
    redis_key = f"oidc_auth_code:{code}"
    redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    ttl = getattr(settings, "OIDC_AUTH_CODE_LIFETIME", 300)
    payload = {
        "email": email,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    redis_client.setex(redis_key, ttl, json.dumps(payload))
    return code


def consume_auth_code(code):
    """Atomically retrieve and delete an authorization code (single-use).
    Uses GETDEL (Redis 6.2+) for a single atomic command."""
    redis_key = f"oidc_auth_code:{code}"
    redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    raw = redis_client.getdel(redis_key)
    if not raw:
        return None
    return json.loads(raw)


def build_id_token(email, client_id, nonce=None):
    """Build a signed OIDC ID token (RS256 JWT)."""
    issuer = get_issuer_url()
    now = int(time.time())
    lifetime = getattr(settings, "OIDC_ID_TOKEN_LIFETIME", 300)
    key_id = getattr(settings, "OIDC_KEY_ID", "attendee-oidc-1")

    payload = {
        "iss": issuer,
        "sub": email,
        "aud": client_id,
        "email": email,
        "iat": now,
        "exp": now + lifetime,
    }
    if nonce:
        payload["nonce"] = nonce

    private_key = get_private_key()
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": key_id})


def validate_client_credentials(client_id, client_secret):
    """
    Check client_id/client_secret against stored GoogleMeetBotLogin credentials.
    Returns True if any active OIDC login has matching credentials.
    Uses constant-time comparison for the secret to prevent timing attacks.
    """
    for login in GoogleMeetBotLogin.objects.filter(is_active=True, sso_mode="oidc"):
        creds = login.get_credentials()
        if not creds:
            continue
        if creds.get("client_id") == client_id and hmac.compare_digest(creds.get("client_secret", ""), client_secret):
            return True
    return False


def get_oidc_discovery():
    """Return the OpenID Connect discovery document."""
    issuer = get_issuer_url()
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "userinfo_endpoint": f"{issuer}/userinfo",
        "jwks_uri": f"{issuer}/jwks",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "claims_supported": ["sub", "email", "iss", "aud", "exp", "iat", "nonce"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
    }


def get_jwks_document():
    """Return the JWKS document for token verification."""
    return get_jwks()


def store_access_token(access_token, email):
    """Store access_token → email mapping in Redis for userinfo lookups."""
    redis_key = f"oidc_access_token:{access_token}"
    redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    ttl = getattr(settings, "OIDC_ID_TOKEN_LIFETIME", 300)
    redis_client.setex(redis_key, ttl, email)


def get_email_for_access_token(access_token):
    """Look up the email associated with an access token."""
    redis_key = f"oidc_access_token:{access_token}"
    redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
    email = redis_client.get(redis_key)
    if email:
        return email.decode() if isinstance(email, bytes) else email
    return None
