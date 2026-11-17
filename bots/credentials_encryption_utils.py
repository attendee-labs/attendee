import logging

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings

logger = logging.getLogger(__name__)

# Key rotation for CREDENTIALS_ENCRYPTION_KEY
#
# 1. Set NEW_CREDENTIALS_ENCRYPTION_KEY to a freshly generated Fernet key and wait until every
#    pod (web, workers, scheduler and bot pods) is running with it. While it is set, new writes are
#    encrypted with the new key and reads accept either key.
# 2. Run `python manage.py rotate_credentials_encryption_key` to re-encrypt every encrypted column
#    with the new key. Run it again with --check right before step 3; it must report 0 rows left.
# 3. In a single rollout, set CREDENTIALS_ENCRYPTION_KEY to the new key and set
#    NEW_CREDENTIALS_ENCRYPTION_KEY to ''.
#
# A malformed NEW_CREDENTIALS_ENCRYPTION_KEY is ignored (with an error log) instead of breaking
# every encrypt and decrypt, and the rotation command refuses to run with it.

MALFORMED_NEW_KEY_LOG_INTERVAL = 1000
_malformed_new_key_count = 0


def is_valid_fernet_key(key):
    try:
        Fernet(key)
        return True
    except (ValueError, TypeError):
        return False


def configured_new_credentials_encryption_key():
    """NEW_CREDENTIALS_ENCRYPTION_KEY as configured, or None when it is unset or equal to the current key."""
    new_key = getattr(settings, "NEW_CREDENTIALS_ENCRYPTION_KEY", None)
    if not new_key or new_key == settings.CREDENTIALS_ENCRYPTION_KEY:
        return None
    return new_key


def new_credentials_encryption_key():
    """The key being rotated to, or None when no rotation is in progress or the configured key is malformed."""
    global _malformed_new_key_count

    new_key = configured_new_credentials_encryption_key()
    if new_key is None:
        return None
    if not is_valid_fernet_key(new_key):
        if _malformed_new_key_count % MALFORMED_NEW_KEY_LOG_INTERVAL == 0:
            logger.error(f"NEW_CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key; ignoring it and using only CREDENTIALS_ENCRYPTION_KEY (occurrence {_malformed_new_key_count + 1})")
        _malformed_new_key_count += 1
        return None
    return new_key


def get_credentials_fernet():
    """Fernet used for runtime encryption and decryption of stored credentials.

    During a rotation it encrypts with the new key and decrypts with either key.
    """
    new_key = new_credentials_encryption_key()
    if new_key is None:
        return Fernet(settings.CREDENTIALS_ENCRYPTION_KEY)
    return MultiFernet([Fernet(new_key), Fernet(settings.CREDENTIALS_ENCRYPTION_KEY)])


def encrypt_credentials(data: bytes) -> bytes:
    return get_credentials_fernet().encrypt(data)


def decrypt_credentials(token: bytes) -> bytes:
    return get_credentials_fernet().decrypt(bytes(token))
