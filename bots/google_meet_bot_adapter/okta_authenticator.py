import logging
import time

import pyotp
import requests

logger = logging.getLogger(__name__)


class OktaLoginError(Exception):
    """Base exception for all Okta login failures."""

    pass


class OktaAuthenticationError(OktaLoginError):
    """Wrong username/password or account locked."""

    pass


class OktaMfaError(OktaLoginError):
    """MFA-related failures: wrong TOTP code, no factor enrolled, expired state token."""

    pass


class OktaSessionError(OktaLoginError):
    """Failed to establish browser session (Okta cookie redirect or Google sign-in)."""

    pass


class OktaAuthenticator:
    """Authenticates against the Okta Authentication API with TOTP MFA.

    Returns a one-time sessionToken that can be exchanged for a browser
    session via /login/sessionCookieRedirect.
    """

    def __init__(self, okta_domain: str, username: str, password: str, totp_secret: str):
        self.okta_domain = okta_domain
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.base_url = f"https://{okta_domain}"

    def authenticate(self) -> str:
        """Run the full Okta auth flow and return a sessionToken."""
        logger.info("Starting Okta primary authentication")
        authn_response = self._primary_auth()

        status = authn_response.get("status")
        if status == "SUCCESS":
            # MFA not required — account doesn't have 2FA
            logger.info("Primary auth succeeded without MFA")
            return authn_response["sessionToken"]

        if status == "MFA_REQUIRED":
            state_token = authn_response["stateToken"]
            factors = authn_response.get("_embedded", {}).get("factors", [])
            totp_factor = self._find_totp_factor(factors)
            logger.info(f"MFA required. Using TOTP factor {totp_factor['id']} (provider: {totp_factor.get('provider', 'unknown')})")
            return self._verify_totp(totp_factor["id"], state_token)

        if status == "LOCKED_OUT":
            raise OktaAuthenticationError(f"Account is locked out. Status: {status}")

        raise OktaAuthenticationError(f"Unexpected authentication status: {status}")

    def _primary_auth(self) -> dict:
        """POST /api/v1/authn with username and password."""
        url = f"{self.base_url}/api/v1/authn"
        payload = {
            "username": self.username,
            "password": self.password,
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            raise OktaLoginError(f"Network error during primary auth: {e}") from e

        if resp.status_code == 401:
            error_summary = resp.json().get("errorSummary", "Authentication failed")
            raise OktaAuthenticationError(f"Invalid credentials: {error_summary}")

        if resp.status_code == 429:
            raise OktaLoginError("Rate limited by Okta. Try again later.")

        if resp.status_code != 200:
            error_summary = resp.json().get("errorSummary", resp.text)
            raise OktaLoginError(f"Okta authn failed (HTTP {resp.status_code}): {error_summary}")

        return resp.json()

    def _find_totp_factor(self, factors: list) -> dict:
        """Find the TOTP factor from the factors list."""
        for factor in factors:
            if factor.get("factorType") == "token:software:totp":
                return factor

        available = [f"{f.get('factorType')}:{f.get('provider')}" for f in factors]
        raise OktaMfaError(f"No TOTP factor enrolled. Available factors: {available}. Ensure a TOTP authenticator (Google Authenticator, Okta Verify) is enrolled.")

    def _verify_totp(self, factor_id: str, state_token: str) -> str:
        """Verify the TOTP code and return a sessionToken."""
        # Strip dashes/spaces — some providers format secrets for readability
        clean_secret = self.totp_secret.replace("-", "").replace(" ", "").upper()
        totp = pyotp.TOTP(clean_secret)
        code = totp.now()
        logger.info(f"Generated TOTP code (expires in ~{totp.interval - (time.time() % totp.interval):.0f}s)")

        url = f"{self.base_url}/api/v1/authn/factors/{factor_id}/verify"
        payload = {
            "stateToken": state_token,
            "passCode": code,
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            raise OktaLoginError(f"Network error during TOTP verify: {e}") from e

        data = resp.json()

        if resp.status_code == 403:
            error_code = data.get("errorCode", "")
            if error_code == "E0000011":
                raise OktaMfaError("State token has expired. Re-authenticate from the beginning.")
            factor_result = data.get("factorResult", "")
            if factor_result == "REJECTED" or "passcode" in data.get("errorSummary", "").lower():
                raise OktaMfaError("TOTP code was rejected. Check your TOTP secret and system clock.")
            raise OktaMfaError(f"MFA verification failed: {data.get('errorSummary', resp.text)}")

        if resp.status_code != 200:
            raise OktaMfaError(f"MFA verify failed (HTTP {resp.status_code}): {data.get('errorSummary', resp.text)}")

        if data.get("status") != "SUCCESS":
            raise OktaMfaError(f"Unexpected MFA status: {data.get('status')}")

        logger.info("TOTP verification succeeded")
        return data["sessionToken"]
