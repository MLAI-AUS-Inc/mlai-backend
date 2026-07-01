import base64
import hashlib
import logging

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

logger = logging.getLogger(__name__)


def _fernet_key_from_secret(secret: str) -> bytes:
    """Derive a urlsafe-base64 Fernet key from an arbitrary secret string."""
    digest = hashlib.sha256((secret or "").encode()).digest()  # 32 bytes
    return base64.urlsafe_b64encode(digest)


def _primary_key() -> bytes:
    """Key used to encrypt NEW values.

    Prefer a dedicated FIELD_ENCRYPTION_KEY so token secrecy is decoupled from
    SECRET_KEY; fall back to a key derived from SECRET_KEY when it is unset.
    """
    field_key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if field_key:
        key = field_key.encode() if isinstance(field_key, str) else field_key
        try:
            Fernet(key)  # validate shape/length early with a clear error
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEY is not a valid Fernet key "
                "(expected urlsafe base64, 32 bytes)."
            ) from exc
        return key
    return _fernet_key_from_secret(getattr(settings, "SECRET_KEY", "") or "")


def _decryption_keys() -> list[bytes]:
    """All keys to try when decrypting, newest first.

    Order: the primary key, then a key derived from the current SECRET_KEY, then
    a key derived from the legacy (historically committed) secret. This lets a
    row written under any of these still decrypt -- so rotating SECRET_KEY or
    introducing FIELD_ENCRYPTION_KEY never bricks existing tokens.
    """
    keys: list[bytes] = [_primary_key()]
    for secret in (
        getattr(settings, "SECRET_KEY", "") or "",
        getattr(settings, "LEGACY_FIELD_ENCRYPTION_SECRET", "") or "",
    ):
        if not secret:
            continue
        derived = _fernet_key_from_secret(secret)
        if derived not in keys:
            keys.append(derived)
    return keys


def _primary_fernet() -> Fernet:
    return Fernet(_primary_key())


def _multifernet() -> MultiFernet:
    return MultiFernet([Fernet(key) for key in _decryption_keys()])


def encrypt_value(value: str) -> str:
    return _primary_fernet().encrypt(str(value).encode()).decode()


def decrypt_value(token: str) -> str:
    return _multifernet().decrypt(token.encode()).decode()


class EncryptedTextField(models.TextField):
    description = "A TextField that is encrypted at rest"

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        try:
            return decrypt_value(value)
        except Exception:
            # The value may be legacy plaintext (pre-encryption) or written under
            # a key we no longer hold. Stay resilient on read -- return as-is --
            # but surface it so the row can be re-encrypted / investigated rather
            # than failing silently.
            logger.warning(
                "EncryptedTextField could not decrypt a stored value; returning it raw. "
                "Run `manage.py reencrypt_secrets` if this is an old key."
            )
            return value

    def get_prep_value(self, value):
        if value is None:
            return value
        return encrypt_value(value)
