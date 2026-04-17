"""
Custom Google Cloud Storage backend with IAM-based URL signing support.

This backend allows signed URL generation using Workload Identity or Compute Engine
default credentials by signing via the IAM signBlob API instead of requiring a
local private key.

Usage:
    Set GCS_USE_IAM_SIGNING=true and ensure the service account has the
    roles/iam.serviceAccountTokenCreator role on itself.
"""

import logging
from datetime import timedelta

from django.conf import settings
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request
from storages.backends.gcloud import GoogleCloudStorage

logger = logging.getLogger(__name__)


class IAMSigningGoogleCloudStorage(GoogleCloudStorage):
    """
    Google Cloud Storage backend that uses IAM-based signing for signed URLs.

    This allows Workload Identity and Compute Engine credentials to generate
    signed URLs without needing a service account JSON key file.

    Requirements:
        - The service account must have roles/iam.serviceAccountTokenCreator
          permission on itself (to call signBlob API)
        - GCS_SERVICE_ACCOUNT_EMAIL must be set to the service account email
    """

    def __init__(self, **settings_kwargs):
        # Extract service_account_email before passing to parent (not a valid parent option)
        self._service_account_email = settings_kwargs.pop("service_account_email", None) or getattr(
            settings, "GCS_SERVICE_ACCOUNT_EMAIL", None
        )
        super().__init__(**settings_kwargs)
        # Initialize credentials for IAM signing
        self._signing_credentials = None

    def _get_access_token(self):
        """
        Get a valid access token for IAM-based signing.
        Uses Application Default Credentials and refreshes if needed.
        """
        if self._signing_credentials is None:
            self._signing_credentials, _ = google_auth_default()

        # Refresh credentials if token is missing or expired
        if not self._signing_credentials.token or (
            self._signing_credentials.expired and self._signing_credentials.expiry
        ):
            self._signing_credentials.refresh(Request())

        return self._signing_credentials.token

    def url(self, name):
        """
        Generate a signed URL using IAM-based signing.
        """
        # If not using querystring auth, return public URL
        if not self.querystring_auth:
            return super().url(name)

        # If no service account email configured, fall back to default behavior
        if not self._service_account_email:
            logger.warning(
                "GCS_SERVICE_ACCOUNT_EMAIL not set, falling back to default signing. "
                "This will fail if using Workload Identity without a key file."
            )
            return super().url(name)

        # Generate signed URL using IAM signing
        blob = self.bucket.blob(name)

        # Get expiration from settings
        expiration = getattr(settings, "GCS_STORAGE_LINK_EXPIRATION_SECONDS", 1800)
        if isinstance(expiration, int):
            expiration = timedelta(seconds=expiration)

        try:
            access_token = self._get_access_token()
            url = blob.generate_signed_url(
                version="v4",
                expiration=expiration,
                method="GET",
                service_account_email=self._service_account_email,
                access_token=access_token,
            )
            return url
        except Exception as e:
            logger.error(f"Failed to generate signed URL using IAM signing: {e}")
            raise
