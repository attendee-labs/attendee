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
        super().__init__(**settings_kwargs)
        self._service_account_email = getattr(
            settings, "GCS_SERVICE_ACCOUNT_EMAIL", None
        ) or settings_kwargs.get("service_account_email")

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
            url = blob.generate_signed_url(
                version="v4",
                expiration=expiration,
                method="GET",
                service_account_email=self._service_account_email,
                access_token=self.credentials.token,
            )
            return url
        except Exception as e:
            logger.error(f"Failed to generate signed URL using IAM signing: {e}")
            raise
