import logging

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings

logger = logging.getLogger(__name__)

# Key rotation for CREDENTIALS_ENCRYPTION_KEY
#
# This works like Django's SECRET_KEY / SECRET_KEY_FALLBACKS: CREDENTIALS_ENCRYPTION_KEY encrypts,
# and CREDENTIALS_ENCRYPTION_KEY_FALLBACKS holds extra keys that can decrypt but never encrypt.
#
# 1. Deploy with the new key added to CREDENTIALS_ENCRYPTION_KEY_FALLBACKS and
#    CREDENTIALS_ENCRYPTION_KEY unchanged. Nothing is encrypted with the new key yet. Wait until no
#    process from before this deploy is still running.
# 2. Deploy with CREDENTIALS_ENCRYPTION_KEY set to the new key and CREDENTIALS_ENCRYPTION_KEY_FALLBACKS
#    set to the old key.
# 3. Once no process from before step 2 is still running, run
#    `python manage.py rotate_credentials_encryption_key` to re-encrypt every encrypted column with
#    the new key.
# 4. Run the command again with --check. When it reports 0 rows on a fallback key, deploy with
#    CREDENTIALS_ENCRYPTION_KEY_FALLBACKS removed. Keep the old key somewhere safe until any
#    database backups taken before step 3 have expired.
#
# A malformed fallback key is ignored (with an error log) instead of breaking every encrypt and
# decrypt, and the rotation command refuses to run with it.

MALFORMED_FALLBACK_KEY_LOG_INTERVAL = 1000
_malformed_fallback_key_count = 0


def is_valid_fernet_key(key):
    try:
        Fernet(key)
        return True
    except (ValueError, TypeError):
        return False


def configured_fallback_credentials_encryption_keys():
    """CREDENTIALS_ENCRYPTION_KEY_FALLBACKS as configured, without the current key."""
    fallback_keys = getattr(settings, "CREDENTIALS_ENCRYPTION_KEY_FALLBACKS", None) or []
    return [key for key in fallback_keys if key and key != settings.CREDENTIALS_ENCRYPTION_KEY]


def fallback_credentials_encryption_keys():
    """The fallback keys that are usable. Malformed ones are logged and left out."""
    global _malformed_fallback_key_count

    valid_keys = []
    for key in configured_fallback_credentials_encryption_keys():
        if is_valid_fernet_key(key):
            valid_keys.append(key)
            continue
        if _malformed_fallback_key_count % MALFORMED_FALLBACK_KEY_LOG_INTERVAL == 0:
            logger.error(f"CREDENTIALS_ENCRYPTION_KEY_FALLBACKS contains a key that is not a valid Fernet key; ignoring it (occurrence {_malformed_fallback_key_count + 1})")
        _malformed_fallback_key_count += 1
    return valid_keys


def get_credentials_fernet():
    """Fernet used for runtime encryption and decryption of stored credentials.

    It always encrypts with CREDENTIALS_ENCRYPTION_KEY and decrypts with it or any fallback key.
    """
    current_fernet = Fernet(settings.CREDENTIALS_ENCRYPTION_KEY)
    fallback_keys = fallback_credentials_encryption_keys()
    if not fallback_keys:
        return current_fernet
    return MultiFernet([current_fernet, *[Fernet(key) for key in fallback_keys]])


def encrypt_credentials(data: bytes) -> bytes:
    return get_credentials_fernet().encrypt(data)


def decrypt_credentials(token: bytes) -> bytes:
    return get_credentials_fernet().decrypt(bytes(token))
