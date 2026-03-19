import base64
import logging
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_private_key = None
_public_key = None


def _load_keys():
    global _private_key, _public_key

    private_pem = getattr(settings, "OIDC_RSA_PRIVATE_KEY", None)
    public_pem = getattr(settings, "OIDC_RSA_PUBLIC_KEY", None)

    if private_pem and public_pem:
        # Handle literal \n from environment variables
        private_pem = private_pem.replace("\\n", "\n")
        public_pem = public_pem.replace("\\n", "\n")
        _private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
        _public_key = serialization.load_pem_public_key(public_pem.encode())
        logger.info("OIDC RSA key pair loaded from settings")
    else:
        is_testing = "test" in sys.argv
        if not settings.DEBUG and not is_testing:
            raise ImproperlyConfigured("OIDC_RSA_PRIVATE_KEY and OIDC_RSA_PUBLIC_KEY must be set in production. " "Generate an RSA key pair and set these environment variables.")
        logger.warning("OIDC_RSA_PRIVATE_KEY / OIDC_RSA_PUBLIC_KEY not set — generating ephemeral key pair.")
        _private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _public_key = _private_key.public_key()


def get_private_key():
    if _private_key is None:
        _load_keys()
    return _private_key


def get_public_key():
    if _public_key is None:
        _load_keys()
    return _public_key


def get_jwks():
    """Return JWKS JSON dict containing the RSA public key."""
    pub = get_public_key()
    pub_numbers = pub.public_numbers()
    key_id = getattr(settings, "OIDC_KEY_ID", "attendee-oidc-1")

    def _int_to_base64url(n, length=None):
        raw = n.to_bytes((n.bit_length() + 7) // 8, byteorder="big")
        if length and len(raw) < length:
            raw = b"\x00" * (length - len(raw)) + raw
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": key_id,
                "n": _int_to_base64url(pub_numbers.n),
                "e": _int_to_base64url(pub_numbers.e),
            }
        ]
    }
