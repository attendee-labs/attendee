import base64
import html
import json
import logging
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import redis
from django.conf import settings
from django.urls import reverse
from lxml import etree
from signxml import (
    CanonicalizationMethod,
    DigestAlgorithm,
    SignatureConstructionMethod,
    SignatureMethod,
    XMLSigner,
)

from bots.bots_api_utils import build_internal_site_url
from bots.models import Bot, BotLogin, BotLoginPlatform

logger = logging.getLogger(__name__)


def get_google_meet_set_cookie_url(session_id):
    # Use build_internal_site_url so that if we have an internal site domain set, we use it.
    base_url = build_internal_site_url(reverse("bot_sso:google_meet_set_cookie"))
    query_params = urlencode({"session_id": session_id})
    google_meet_set_cookie_url = f"{base_url}?{query_params}"
    return google_meet_set_cookie_url


def create_google_meet_sign_in_session(bot: Bot, google_meet_bot_login: BotLogin):
    session_id = str(uuid.uuid4())
    redis_key = f"google_meet_sign_in_session:{session_id}"
    redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)

    session_data = {
        "bot_object_id": bot.object_id,
        "google_meet_bot_login_object_id": google_meet_bot_login.object_id,
    }

    # Save for 30 minutes.
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
    google_meet_bot_login = BotLogin.objects.filter(object_id=google_meet_bot_login_object_id, group__project=bot.project, group__platform=BotLoginPlatform.GOOGLE_MEET).first()
    if not google_meet_bot_login:
        logger.info(f"No google_meet_bot_login found for google_meet_sign_in_session: {session_id}. Data: {session_data}")
        return None

    if not bot:
        logger.info(f"No bot found for google_meet_sign_in_session: {session_id}. Data: {session_data}")
        return None

    return google_meet_bot_login


IDP_ENTITY_ID = "https://idp.attendee.local"

SAML_PROTOCOL_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML_ASSERTION_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
XML_SIGNATURE_NS = "http://www.w3.org/2000/09/xmldsig#"
XML_SCHEMA_INSTANCE_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_SCHEMA_NS = "http://www.w3.org/2001/XMLSchema"

NAMEID_FORMAT_EMAILADDRESS = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
NAMEID_FORMAT_ENTITY = "urn:oasis:names:tc:SAML:2.0:nameid-format:entity"
ATTRIBUTE_NAME_FORMAT_URI = "urn:oasis:names:tc:SAML:2.0:attrname-format:uri"
SUBJECT_CONFIRMATION_BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
AUTHN_CONTEXT_PASSWORD_PROTECTED_TRANSPORT = "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"

ASSERTION_TTL = timedelta(minutes=5)

NSP = {
    "samlp": SAML_PROTOCOL_NS,
    "saml": SAML_ASSERTION_NS,
    "ds": XML_SIGNATURE_NS,
}

OUTPUT_NSMAP = {
    "samlp": SAML_PROTOCOL_NS,
    "saml": SAML_ASSERTION_NS,
    "ds": XML_SIGNATURE_NS,
    "xsi": XML_SCHEMA_INSTANCE_NS,
    "xs": XML_SCHEMA_NS,
}

# These match the URI-format attribute mappings pysaml2 used for the
# previous identity dictionary.
EMAIL_ATTRIBUTES = (
    (
        "mail",
        "urn:oid:0.9.2342.19200300.100.1.3",
    ),
    (
        "email",
        "urn:oid:1.2.840.113549.1.9.1.1",
    ),
    (
        "uid",
        "urn:oid:0.9.2342.19200300.100.1.1",
    ),
)


def _qname(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _new_saml_id() -> str:
    return f"_{uuid.uuid4().hex}"


def _format_saml_time(value: datetime) -> str:
    """
    Format a timezone-aware datetime as a SAML-compatible UTC timestamp.
    """
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _inflate_redirect_binding(saml_request_b64: str) -> bytes:
    """
    Base64-decode and raw-DEFLATE-inflate an HTTP-Redirect SAMLRequest.
    """
    raw = base64.b64decode(saml_request_b64)
    return zlib.decompress(raw, -15)


def _parse_authn_request(xml_bytes: bytes):
    """
    Extract these values from an AuthnRequest:

      - request_id
      - issuer, which is the SP entity ID
      - acs_url
      - protocol_binding
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        dtd_validation=False,
        no_network=True,
        recover=False,
        remove_comments=True,
        remove_pis=True,
        huge_tree=False,
    )

    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Unable to parse AuthnRequest XML: {exc}") from exc

    if root.getroottree().docinfo.doctype:
        raise ValueError("AuthnRequest must not contain a DOCTYPE")

    expected_tag = _qname(
        SAML_PROTOCOL_NS,
        "AuthnRequest",
    )
    if root.tag != expected_tag:
        raise ValueError("Not a SAML 2.0 AuthnRequest")

    issuer_element = root.find(
        "saml:Issuer",
        namespaces=NSP,
    )

    issuer = None
    if issuer_element is not None and issuer_element.text and issuer_element.text.strip():
        issuer = issuer_element.text.strip()

    return {
        "request_id": root.get("ID"),
        "issuer": issuer,
        "acs_url": root.get("AssertionConsumerServiceURL"),
        "protocol_binding": root.get("ProtocolBinding"),
    }


def _add_issuer(parent) -> None:
    issuer = etree.SubElement(
        parent,
        _qname(SAML_ASSERTION_NS, "Issuer"),
        Format=NAMEID_FORMAT_ENTITY,
    )
    issuer.text = IDP_ENTITY_ID


def _add_signature_placeholder(parent) -> None:
    """
    SignXML replaces this element with the generated ds:Signature.

    Its position is important because the SAML schema requires Signature
    immediately after Issuer in both Response and Assertion.
    """
    etree.SubElement(
        parent,
        _qname(XML_SIGNATURE_NS, "Signature"),
        Id="placeholder",
    )


def _new_xml_signer() -> XMLSigner:
    return XMLSigner(
        method=SignatureConstructionMethod.enveloped,
        signature_algorithm=SignatureMethod.RSA_SHA256,
        digest_algorithm=DigestAlgorithm.SHA256,
        c14n_algorithm=(CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0),
    )


def _sign_element(
    element,
    *,
    element_id: str,
    cert: str,
    private_key: str,
):
    return _new_xml_signer().sign(
        element,
        key=private_key,
        cert=cert,
        reference_uri=f"#{element_id}",
        id_attribute="ID",
    )


def _add_email_attribute_statement(
    assertion,
    email_to_sign_in: str,
) -> None:
    attribute_statement = etree.SubElement(
        assertion,
        _qname(SAML_ASSERTION_NS, "AttributeStatement"),
    )

    for friendly_name, attribute_name in EMAIL_ATTRIBUTES:
        attribute = etree.SubElement(
            attribute_statement,
            _qname(SAML_ASSERTION_NS, "Attribute"),
            Name=attribute_name,
            NameFormat=ATTRIBUTE_NAME_FORMAT_URI,
            FriendlyName=friendly_name,
        )

        attribute_value = etree.SubElement(
            attribute,
            _qname(SAML_ASSERTION_NS, "AttributeValue"),
        )
        attribute_value.set(
            _qname(XML_SCHEMA_INSTANCE_NS, "type"),
            "xs:string",
        )
        attribute_value.text = email_to_sign_in


def _build_signed_saml_response_xml(
    *,
    email_to_sign_in: str,
    cert: str,
    private_key: str,
    request_id: str,
    acs_url: str,
    sp_entity_id: str,
) -> bytes:
    now = datetime.now(timezone.utc)
    expires_at = now + ASSERTION_TTL

    issue_instant = _format_saml_time(now)
    not_on_or_after = _format_saml_time(expires_at)

    response_id = _new_saml_id()
    assertion_id = _new_saml_id()
    session_index = _new_saml_id()

    response = etree.Element(
        _qname(SAML_PROTOCOL_NS, "Response"),
        nsmap=OUTPUT_NSMAP,
        ID=response_id,
        Version="2.0",
        IssueInstant=issue_instant,
        Destination=acs_url,
        InResponseTo=request_id,
    )

    _add_issuer(response)
    _add_signature_placeholder(response)

    status = etree.SubElement(
        response,
        _qname(SAML_PROTOCOL_NS, "Status"),
    )
    etree.SubElement(
        status,
        _qname(SAML_PROTOCOL_NS, "StatusCode"),
        Value=STATUS_SUCCESS,
    )

    assertion = etree.SubElement(
        response,
        _qname(SAML_ASSERTION_NS, "Assertion"),
        ID=assertion_id,
        Version="2.0",
        IssueInstant=issue_instant,
    )

    _add_issuer(assertion)
    _add_signature_placeholder(assertion)

    subject = etree.SubElement(
        assertion,
        _qname(SAML_ASSERTION_NS, "Subject"),
    )

    name_id = etree.SubElement(
        subject,
        _qname(SAML_ASSERTION_NS, "NameID"),
        Format=NAMEID_FORMAT_EMAILADDRESS,
    )
    name_id.text = email_to_sign_in

    subject_confirmation = etree.SubElement(
        subject,
        _qname(SAML_ASSERTION_NS, "SubjectConfirmation"),
        Method=SUBJECT_CONFIRMATION_BEARER,
    )

    etree.SubElement(
        subject_confirmation,
        _qname(
            SAML_ASSERTION_NS,
            "SubjectConfirmationData",
        ),
        InResponseTo=request_id,
        Recipient=acs_url,
        NotOnOrAfter=not_on_or_after,
    )

    conditions = etree.SubElement(
        assertion,
        _qname(SAML_ASSERTION_NS, "Conditions"),
        NotBefore=issue_instant,
        NotOnOrAfter=not_on_or_after,
    )

    audience_restriction = etree.SubElement(
        conditions,
        _qname(SAML_ASSERTION_NS, "AudienceRestriction"),
    )

    audience = etree.SubElement(
        audience_restriction,
        _qname(SAML_ASSERTION_NS, "Audience"),
    )
    audience.text = sp_entity_id

    authn_statement = etree.SubElement(
        assertion,
        _qname(SAML_ASSERTION_NS, "AuthnStatement"),
        AuthnInstant=issue_instant,
        SessionIndex=session_index,
    )

    authn_context = etree.SubElement(
        authn_statement,
        _qname(SAML_ASSERTION_NS, "AuthnContext"),
    )

    authn_context_class_ref = etree.SubElement(
        authn_context,
        _qname(
            SAML_ASSERTION_NS,
            "AuthnContextClassRef",
        ),
    )
    authn_context_class_ref.text = AUTHN_CONTEXT_PASSWORD_PROTECTED_TRANSPORT

    authenticating_authority = etree.SubElement(
        authn_context,
        _qname(
            SAML_ASSERTION_NS,
            "AuthenticatingAuthority",
        ),
    )
    authenticating_authority.text = IDP_ENTITY_ID

    _add_email_attribute_statement(
        assertion,
        email_to_sign_in,
    )

    # Sign the assertion before signing the response. The response
    # signature will therefore cover the complete signed assertion.
    signed_assertion = _sign_element(
        assertion,
        element_id=assertion_id,
        cert=cert,
        private_key=private_key,
    )
    response.replace(assertion, signed_assertion)

    signed_response = _sign_element(
        response,
        element_id=response_id,
        cert=cert,
        private_key=private_key,
    )

    # Do not pretty-print signed XML. Added whitespace can invalidate
    # canonicalized XML signatures.
    return etree.tostring(
        signed_response,
        encoding="UTF-8",
        xml_declaration=False,
        pretty_print=False,
    )


def _html_auto_post_form(
    action_url: str,
    saml_response_b64: str,
    relay_state: str | None,
) -> str:
    """
    Return an HTML page that automatically POSTs SAMLResponse and,
    when present, RelayState to the SP's ACS.
    """
    escaped_action_url = html.escape(
        action_url,
        quote=True,
    )
    escaped_saml_response = html.escape(
        saml_response_b64,
        quote=True,
    )

    relay_state_input = ""
    if relay_state is not None:
        escaped_relay_state = html.escape(
            str(relay_state),
            quote=True,
        )
        relay_state_input = f'<input type="hidden" name="RelayState" value="{escaped_relay_state}"/>'

    return f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8"/>
    <title>SAML Post</title>
  </head>
  <body onload="document.forms[0].submit()">
    <form method="post" action="{escaped_action_url}">
      <input
        type="hidden"
        name="SAMLResponse"
        value="{escaped_saml_response}"
      />
      {relay_state_input}
      <noscript>
        <p>JavaScript is disabled. Click the button below to continue.</p>
        <button type="submit">Continue</button>
      </noscript>
    </form>
  </body>
</html>"""


def _build_sign_in_saml_response(
    saml_request_b64: str,
    email_to_sign_in: str,
    cert: str,
    private_key: str,
) -> tuple[str, str]:
    try:
        xml_bytes = _inflate_redirect_binding(saml_request_b64)
        authn_request = _parse_authn_request(xml_bytes)
    except Exception as exc:
        raise ValueError(f"Failed to decode/parse SAMLRequest: {exc}") from exc

    acs_url = authn_request.get("acs_url")
    sp_entity_id = authn_request.get("issuer")
    request_id = authn_request.get("request_id")

    if not acs_url:
        raise ValueError("AuthnRequest missing AssertionConsumerServiceURL")

    if not sp_entity_id:
        raise ValueError("AuthnRequest missing Issuer")

    if not request_id:
        raise ValueError("AuthnRequest missing ID")

    try:
        response_xml = _build_signed_saml_response_xml(
            email_to_sign_in=email_to_sign_in,
            cert=cert,
            private_key=private_key,
            request_id=request_id,
            acs_url=acs_url,
            sp_entity_id=sp_entity_id,
        )
    except Exception as exc:
        raise ValueError(f"Failed to build/sign SAML response: {exc}") from exc

    saml_response_b64 = base64.b64encode(response_xml).decode("ascii")

    return saml_response_b64, acs_url
