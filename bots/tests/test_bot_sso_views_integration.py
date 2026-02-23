import base64
import os
import uuid
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import redis
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization
from bots.bot_sso_utils import create_google_meet_sign_in_session
from bots.models import Bot, GoogleMeetBotLogin, GoogleMeetBotLoginGroup, Project


def _generate_rsa_key_and_self_signed_cert():
    # 1) RSA private key (unencrypted, PKCS#1 -> BEGIN RSA PRIVATE KEY)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,  # => "BEGIN RSA PRIVATE KEY"
        encryption_algorithm=serialization.NoEncryption(),
    )

    # 2) Minimal self-signed cert that matches the key
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Attendee Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "attendee-test.local"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))  # ~10 years
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    return cert_pem.decode("utf-8"), private_pem.decode("utf-8")


TEST_CERT, TEST_PRIVATE_KEY = _generate_rsa_key_and_self_signed_cert()


def _generate_saml_authn_request(
    request_id: str,
    sp_entity_id: str,
    acs_url: str,
) -> str:
    """
    Generate a SAML AuthnRequest XML and encode it for HTTP-Redirect binding.
    Returns base64-encoded, deflated SAMLRequest parameter.
    """
    # Build the AuthnRequest XML
    namespaces = {
        "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
        "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    }

    # Register namespaces
    for prefix, uri in namespaces.items():
        ET.register_namespace(prefix, uri)

    # Create AuthnRequest element
    authn_request = ET.Element(
        f"{{{namespaces['samlp']}}}AuthnRequest",
        attrib={
            "ID": request_id,
            "Version": "2.0",
            "IssueInstant": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ProtocolBinding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            "AssertionConsumerServiceURL": acs_url,
        },
    )

    # Add Issuer element
    issuer = ET.SubElement(authn_request, f"{{{namespaces['saml']}}}Issuer")
    issuer.text = sp_entity_id

    # Convert to XML string
    xml_string = ET.tostring(authn_request, encoding="utf-8")

    # Deflate and base64 encode (HTTP-Redirect binding)
    compressed = zlib.compress(xml_string)[2:-4]  # Raw DEFLATE (strip zlib header/trailer)
    b64_encoded = base64.b64encode(compressed).decode("ascii")

    return b64_encoded


class BotSsoViewsIntegrationTest(TransactionTestCase):
    """Integration tests for bot SSO views"""

    def setUp(self):
        """Set up test environment"""
        # Create organization, project, and bot
        self.organization = Organization.objects.create(name="Test Organization", centicredits=10000)
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
        )

        # Create GoogleMeetBotLoginGroup and GoogleMeetBotLogin
        self.google_meet_bot_login_group = GoogleMeetBotLoginGroup.objects.create(project=self.project)
        self.google_meet_bot_login = GoogleMeetBotLogin.objects.create(
            group=self.google_meet_bot_login_group,
            workspace_domain="test-workspace.com",
            email="test-bot@test-workspace.com",
        )

        # Set credentials for the GoogleMeetBotLogin
        self.google_meet_bot_login.set_credentials(
            {
                "cert": TEST_CERT,
                "private_key": TEST_PRIVATE_KEY,
            }
        )

        # Set up Redis URL environment variable if not set
        if not os.getenv("REDIS_URL"):
            os.environ["REDIS_URL"] = "redis://localhost:6379/0"

        # Create a test client
        self.client = Client()

        # Generate test SAML parameters
        self.request_id = f"_test_{uuid.uuid4()}"
        self.sp_entity_id = "https://test-sp.example.com"
        self.acs_url = "https://test-sp.example.com/acs"

    def tearDown(self):
        """Clean up Redis after each test"""
        # Clean up any Redis keys created during tests
        redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
        # Get all keys matching our pattern and delete them
        keys = redis_client.keys("google_meet_sign_in_session:*")
        if keys:
            redis_client.delete(*keys)

    def test_set_cookie_view_with_valid_session(self):
        """Test GoogleMeetSetCookieView with a valid session"""
        # Create a session in Redis
        session_id = create_google_meet_sign_in_session(self.bot, self.google_meet_bot_login)

        # Make a GET request to the set cookie endpoint
        url = reverse("bot_sso:google_meet_set_cookie")
        response = self.client.get(url, {"session_id": session_id})

        # Assert the response is successful
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Google Meet Set Cookie")

        # Assert the cookie is set
        self.assertIn("google_meet_sign_in_session_id", response.cookies)
        cookie = response.cookies["google_meet_sign_in_session_id"]
        self.assertEqual(cookie.value, session_id)
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_set_cookie_view_without_session_id(self):
        """Test GoogleMeetSetCookieView without session_id parameter"""
        url = reverse("bot_sso:google_meet_set_cookie")
        response = self.client.get(url)

        # Assert the response is a bad request
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Could not set cookie")

    def test_set_cookie_view_with_invalid_session(self):
        """Test GoogleMeetSetCookieView with a non-existent session"""
        url = reverse("bot_sso:google_meet_set_cookie")
        response = self.client.get(url, {"session_id": "invalid-session-id"})

        # Assert the response is a bad request
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Could not set cookie")

    @patch("bots.bot_sso_utils.XMLSEC_BINARY", "/usr/bin/xmlsec1")
    def test_sign_in_view_with_valid_saml_request(self):
        """Test GoogleMeetSignInView with a valid SAML AuthnRequest"""
        # Create a session in Redis
        session_id = create_google_meet_sign_in_session(self.bot, self.google_meet_bot_login)

        # Set the cookie (simulate the set cookie flow)
        self.client.cookies["google_meet_sign_in_session_id"] = session_id

        # Generate a SAML AuthnRequest
        saml_request_b64 = _generate_saml_authn_request(
            request_id=self.request_id,
            sp_entity_id=self.sp_entity_id,
            acs_url=self.acs_url,
        )

        # Make a GET request to the sign-in endpoint
        url = reverse("bot_sso:google_meet_sign_in")
        response = self.client.get(
            url,
            {
                "SAMLRequest": saml_request_b64,
                "RelayState": "test_relay_state",
            },
        )

        # Assert the response is successful
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/html")

        # Assert the response contains an auto-submitting form
        content = response.content.decode()
        self.assertIn("<form", content)
        self.assertIn(f'action="{self.acs_url}"', content)
        self.assertIn('name="SAMLResponse"', content)
        self.assertIn('name="RelayState"', content)
        self.assertIn('value="test_relay_state"', content)
        self.assertIn("document.forms[0].submit()", content)

    def test_sign_in_view_without_cookie(self):
        """Test GoogleMeetSignInView without the session cookie"""
        # Generate a SAML AuthnRequest
        saml_request_b64 = _generate_saml_authn_request(
            request_id=self.request_id,
            sp_entity_id=self.sp_entity_id,
            acs_url=self.acs_url,
        )

        # Make a GET request without setting the cookie
        url = reverse("bot_sso:google_meet_sign_in")
        response = self.client.get(url, {"SAMLRequest": saml_request_b64})

        # Assert the response is a bad request
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Could not sign in")

    def test_sign_in_view_with_invalid_cookie(self):
        """Test GoogleMeetSignInView with an invalid session cookie"""
        # Set an invalid cookie
        self.client.cookies["google_meet_sign_in_session_id"] = "invalid-session-id"

        # Generate a SAML AuthnRequest
        saml_request_b64 = _generate_saml_authn_request(
            request_id=self.request_id,
            sp_entity_id=self.sp_entity_id,
            acs_url=self.acs_url,
        )

        # Make a GET request
        url = reverse("bot_sso:google_meet_sign_in")
        response = self.client.get(url, {"SAMLRequest": saml_request_b64})

        # Assert the response is a bad request
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Could not sign in")

    def test_sign_in_view_without_saml_request(self):
        """Test GoogleMeetSignInView without SAMLRequest parameter"""
        # Create a session and set cookie
        session_id = create_google_meet_sign_in_session(self.bot, self.google_meet_bot_login)
        self.client.cookies["google_meet_sign_in_session_id"] = session_id

        # Make a GET request without SAMLRequest
        url = reverse("bot_sso:google_meet_sign_in")
        response = self.client.get(url)

        # Assert the response is a bad request
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Missing SAMLRequest")

    @patch("bots.bot_sso_utils.XMLSEC_BINARY", "/usr/bin/xmlsec1")
    def test_sign_in_view_with_invalid_cert_or_key(self):
        """Test GoogleMeetSignInView with invalid certificate or private key"""
        # Create a new bot login with invalid credentials
        invalid_bot_login = GoogleMeetBotLogin.objects.create(
            group=self.google_meet_bot_login_group,
            workspace_domain="invalid-workspace.com",
            email="invalid-bot@invalid-workspace.com",
        )
        invalid_bot_login.set_credentials(
            {
                "cert": "INVALID_CERT",
                "private_key": "INVALID_KEY",
            }
        )

        # Create a session with the invalid bot login
        session_id = create_google_meet_sign_in_session(self.bot, invalid_bot_login)
        self.client.cookies["google_meet_sign_in_session_id"] = session_id

        # Generate a SAML AuthnRequest
        saml_request_b64 = _generate_saml_authn_request(
            request_id=self.request_id,
            sp_entity_id=self.sp_entity_id,
            acs_url=self.acs_url,
        )

        # Make a GET request
        url = reverse("bot_sso:google_meet_sign_in")
        response = self.client.get(url, {"SAMLRequest": saml_request_b64})

        # Assert the response is a bad request
        self.assertEqual(response.status_code, 400)
        self.assertIn("Private Key or Cert may be invalid", response.content.decode())

    def test_sign_out_view(self):
        """Test GoogleMeetSignOutView"""
        url = reverse("bot_sso:google_meet_sign_out")
        response = self.client.get(url)

        # Assert the response is successful
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Signed Out Successfully")

    @patch("bots.bot_sso_utils.XMLSEC_BINARY", "/usr/bin/xmlsec1")
    def test_full_sso_flow_end_to_end(self):
        """Test the complete SSO flow from session creation to SAML response"""
        # Step 1: Create a session in Redis
        session_id = create_google_meet_sign_in_session(self.bot, self.google_meet_bot_login)

        # Verify session is created in Redis
        redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
        redis_key = f"google_meet_sign_in_session:{session_id}"
        self.assertTrue(redis_client.exists(redis_key))

        # Step 2: Set the cookie
        set_cookie_url = reverse("bot_sso:google_meet_set_cookie")
        set_cookie_response = self.client.get(set_cookie_url, {"session_id": session_id})

        self.assertEqual(set_cookie_response.status_code, 200)
        self.assertIn("google_meet_sign_in_session_id", set_cookie_response.cookies)

        # Step 3: Perform SAML sign-in
        saml_request_b64 = _generate_saml_authn_request(
            request_id=self.request_id,
            sp_entity_id=self.sp_entity_id,
            acs_url=self.acs_url,
        )

        sign_in_url = reverse("bot_sso:google_meet_sign_in")
        sign_in_response = self.client.get(
            sign_in_url,
            {
                "SAMLRequest": saml_request_b64,
                "RelayState": "end_to_end_test",
            },
        )

        # Assert sign-in is successful
        self.assertEqual(sign_in_response.status_code, 200)

        # Verify the SAML response contains expected elements
        content = sign_in_response.content.decode()
        self.assertIn(f'action="{self.acs_url}"', content)
        self.assertIn('name="SAMLResponse"', content)
        self.assertIn('name="RelayState"', content)
        self.assertIn('value="end_to_end_test"', content)

        # Extract and verify SAMLResponse is base64-encoded
        import re

        saml_response_match = re.search(r'name="SAMLResponse" value="([^"]+)"', content)
        self.assertIsNotNone(saml_response_match)
        saml_response_b64 = saml_response_match.group(1)

        # Verify it's valid base64
        try:
            saml_response_xml = base64.b64decode(saml_response_b64)
            # Verify it's valid XML
            root = ET.fromstring(saml_response_xml)
            # Should be a SAML Response
            self.assertIn("Response", root.tag)
        except Exception as e:
            self.fail(f"SAMLResponse is not valid: {e}")

        # Step 4: Test sign-out
        sign_out_url = reverse("bot_sso:google_meet_sign_out")
        sign_out_response = self.client.get(sign_out_url)
        self.assertEqual(sign_out_response.status_code, 200)
