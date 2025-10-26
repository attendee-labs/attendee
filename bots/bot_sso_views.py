


import base64
import logging
import os
import zlib
import xml.etree.ElementTree as ET
from datetime import timedelta

from django.http import HttpResponse, HttpResponseBadRequest
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

# pysaml2
from saml2 import BINDING_HTTP_POST
from saml2.config import IdPConfig
from saml2.server import Server
from saml2.saml import NameID, NAMEID_FORMAT_EMAILADDRESS

logger = logging.getLogger(__name__)

# =========================
# CONFIG — edit these
# =========================
# =========================
# CONSTANTS — edit these
# =========================
EMAIL_TO_SIGN_IN = ""
IDP_ENTITY_ID = "https://idp.attendee.local"  # Your IdP entityID (can be any stable URL you control)
IDP_SSO_URL = "https://idp.attendee.local/sso"  # Dummy SSO endpoint to satisfy pysaml2 config
CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"
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


def _build_idp_server(sp_entity_id: str, acs_url: str) -> Server:
    """
    Construct a minimal pysaml2 IdP Server instance, injecting the SP's metadata inline
    so pysaml2 can resolve the SP entry (avoids KeyError lookups).
    """
    sp_md_xml = SP_MD_TEMPLATE.format(sp_entity_id=sp_entity_id, acs_url=acs_url)

    conf = {
        "entityid": IDP_ENTITY_ID,
        "xmlsec_binary": XMLSEC_BINARY,
        "key_file": KEY_FILE,
        "cert_file": CERT_FILE,
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
    rs_input = (
        f'<input type="hidden" name="RelayState" value="{relay_state}"/>'
        if relay_state is not None
        else ""
    )
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


@method_decorator(csrf_exempt, name="dispatch")
class GoogleMeetSignInView(View):
    """
    GET endpoint that receives a SAML AuthnRequest via HTTP-Redirect binding and
    returns an auto-submitting HTML form that POSTs a signed SAMLResponse to the ACS.
    """

    def get(self, request):
        # 1) Collect inputs
        saml_request_b64 = request.GET.get("SAMLRequest")
        relay_state = request.GET.get("RelayState")

        if not saml_request_b64:
            return HttpResponseBadRequest("Missing SAMLRequest")

        # 2) Inflate + parse the AuthnRequest
        try:
            xml_bytes = _inflate_redirect_binding(saml_request_b64)
            authn = _parse_authn_request(xml_bytes)
            logger.info("Parsed AuthnRequest: %s", authn)
        except Exception as e:
            logger.exception("Failed to decode/parse SAMLRequest")
            return HttpResponseBadRequest(f"Bad SAMLRequest: {e}")

        acs_url = authn.get("acs_url")
        sp_entity_id = authn.get("issuer")
        in_response_to = authn.get("request_id")

        if not acs_url:
            return HttpResponseBadRequest("AuthnRequest missing AssertionConsumerServiceURL")
        if not sp_entity_id:
            return HttpResponseBadRequest("AuthnRequest missing Issuer")
        if not in_response_to:
            return HttpResponseBadRequest("AuthnRequest missing ID")

        # 3) Build IdP server with inline SP metadata
        try:
            idp = _build_idp_server(sp_entity_id, acs_url)
        except Exception as e:
            logger.exception("Failed to build IdP server")
            return HttpResponseBadRequest(f"Failed to initialize IdP: {e}")

        # 4) Build a NameID and (optionally) attributes for the subject
        # Many SPs (incl. Google) are fine with just NameID. Attributes are optional.
        name_id_obj = NameID(format=NAMEID_FORMAT_EMAILADDRESS, text=EMAIL_TO_SIGN_IN)
        identity = {
            # Keep this minimal; you can also set identity = {}
            "mail": [EMAIL_TO_SIGN_IN],
            "email": [EMAIL_TO_SIGN_IN],
            "uid": [EMAIL_TO_SIGN_IN],
        }

        # 5) Create and sign the SAMLResponse
        try:
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

        except Exception as e:
            logger.exception("Failed to create SAMLResponse")
            return HttpResponseBadRequest(f"Failed to create SAMLResponse: {e}")

        # 6) Return auto-posting HTML to the ACS
        html = _html_auto_post_form(acs_url, saml_response_b64, relay_state)
        return HttpResponse(html, content_type="text/html")
