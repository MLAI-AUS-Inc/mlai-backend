import base64
import hashlib
import json
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

CREDENTIAL_ENVELOPE_PREFIX = "mlai-enc:v1:"
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class CredentialEncryptionError(ValueError):
    """Raised instead of returning plaintext when credential decryption fails."""


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


def _configured_keyring() -> tuple[dict[str, Fernet], str]:
    raw = str(getattr(settings, "CONNECTOR_CREDENTIAL_KEYS", "") or "").strip()
    active_key_id = str(
        getattr(settings, "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID", "") or ""
    ).strip()
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ImproperlyConfigured(
                "CONNECTOR_CREDENTIAL_KEYS must be a JSON object"
            ) from exc
        if not isinstance(decoded, dict) or not decoded:
            raise ImproperlyConfigured(
                "CONNECTOR_CREDENTIAL_KEYS must be a non-empty JSON object"
            )
        keyring: dict[str, Fernet] = {}
        for key_id, encoded_key in decoded.items():
            key_id = str(key_id)
            if not KEY_ID_PATTERN.fullmatch(key_id):
                raise ImproperlyConfigured(
                    f"Invalid connector credential key ID: {key_id}"
                )
            try:
                keyring[key_id] = Fernet(str(encoded_key).encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise ImproperlyConfigured(
                    f"Connector credential key {key_id} is not a valid Fernet key"
                ) from exc
        if not active_key_id or active_key_id not in keyring:
            raise ImproperlyConfigured(
                "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID must identify a configured key"
            )
        return keyring, active_key_id

    if not bool(getattr(settings, "IS_LOCAL_ENV", False)):
        raise ImproperlyConfigured(
            "Production requires CONNECTOR_CREDENTIAL_KEYS and "
            "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID"
        )
    return {"local": _primary_fernet()}, "local"


def encrypt_value(value: str) -> str:
    return _primary_fernet().encrypt(str(value).encode()).decode()


def decrypt_value(token: str) -> str:
    return _multifernet().decrypt(token.encode()).decode()


def encrypt_credential_value(value: str) -> str:
    if value in (None, ""):
        return value
    keyring, active_key_id = _configured_keyring()
    token = keyring[active_key_id].encrypt(str(value).encode("utf-8")).decode("ascii")
    return f"{CREDENTIAL_ENVELOPE_PREFIX}{active_key_id}:{token}"


def decrypt_credential_value(value: str) -> str:
    if value in (None, ""):
        return value
    raw = str(value)
    if raw.startswith(CREDENTIAL_ENVELOPE_PREFIX):
        remainder = raw[len(CREDENTIAL_ENVELOPE_PREFIX):]
        try:
            key_id, token = remainder.split(":", 1)
        except ValueError as exc:
            raise CredentialEncryptionError(
                "Malformed connector credential envelope"
            ) from exc
        keyring, _ = _configured_keyring()
        fernet = keyring.get(key_id)
        if fernet is None:
            raise CredentialEncryptionError(
                f"Connector credential references unavailable key ID {key_id}"
            )
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialEncryptionError(
                "Connector credential decryption failed"
            ) from exc

    try:
        return decrypt_value(raw)
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise CredentialEncryptionError(
            "Unversioned connector credential could not be decrypted"
        ) from exc


class EncryptedTextField(models.TextField):
    description = "A versioned TextField encrypted at rest"

    def from_db_value(self, value, expression, connection):
        return decrypt_credential_value(value)

    def get_prep_value(self, value):
        return encrypt_credential_value(value)
