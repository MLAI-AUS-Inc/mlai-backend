"""Closed CMS scope and context-bound encrypted credentials; no network effects."""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from integrations.fields import encrypt_credential_value, decrypt_credential_value, CredentialEncryptionError
from .website_channel_observation import canonical_domain

CMS_PLATFORMS = frozenset({"wordpress", "ghost"})
BINDING_FIELDS = frozenset({"schema_version", "channel", "platform", "company_id", "organization_id",
    "owner_user_id", "connection_id", "generation", "public_origin", "api_origin"})


class CmsContractError(ValueError):
    """A fixed safe diagnostic that never includes credentials or provider output."""


def canonical_origin(value):
    """Normalize an explicitly selected HTTPS origin and optional CMS subdirectory."""
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or any(ord(c) < 33 for c in value):
        raise CmsContractError("Invalid CMS origin")
    try:
        parsed = urlsplit(value)
        host = canonical_domain(parsed.hostname or "")
        path = parsed.path or "/"
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or
                parsed.port not in {None, 443} or parsed.query or parsed.fragment or
                not path.startswith("/") or "\\" in path or "%" in path or
                any(part in {".", ".."} for part in path.split("/")) or "//" in path):
            raise CmsContractError("Invalid CMS origin")
        return urlunsplit(("https", host, path.rstrip("/") + "/", "", ""))
    except (ValueError, UnicodeError):
        raise CmsContractError("Invalid CMS origin") from None


def validate_binding(value):
    """Require a typed current provider scope without accepting page/worker claims."""
    if (not isinstance(value, dict) or set(value) != BINDING_FIELDS or
            type(value.get("schema_version")) is not int or value["schema_version"] != 1 or
            value.get("channel") != "cms" or not isinstance(value.get("platform"), str) or value["platform"] not in CMS_PLATFORMS or
            type(value.get("generation")) is not int or not 1 <= value["generation"] < 10**12):
        raise CmsContractError("Invalid CMS binding")
    try:
        for key in ("company_id", "connection_id"):
            if not isinstance(value[key], str) or str(UUID(value[key])) != value[key]:
                raise CmsContractError("Invalid CMS binding")
        for key in ("organization_id", "owner_user_id"):
            if not isinstance(value[key], str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value[key]):
                raise CmsContractError("Invalid CMS binding")
        public, api = canonical_origin(value["public_origin"]), canonical_origin(value["api_origin"])
        if public != value["public_origin"] or api != value["api_origin"]:
            raise CmsContractError("Invalid CMS binding")
        if value["platform"] == "wordpress" and urlsplit(public).hostname != urlsplit(api).hostname:
            raise CmsContractError("WordPress API origin requires the selected website host")
    except (ValueError, TypeError, AttributeError):
        raise CmsContractError("Invalid CMS binding") from None
    return deepcopy(value)


@dataclass(frozen=True)
class CmsCredential:
    """Short-lived private plaintext for one checked backend request; never log it."""

    platform: str
    username: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)


def _credential(platform, username, secret):
    if not isinstance(username, str) or not isinstance(secret, str):
        raise CmsContractError("Invalid CMS credential")
    if platform == "wordpress":
        secret = secret.replace(" ", "")
        if (not 0 < len(username) <= 150 or ":" in username or any(ord(c) < 33 for c in username) or
                not 16 <= len(secret) <= 200 or any(not 33 <= ord(c) < 127 for c in secret)):
            raise CmsContractError("Invalid CMS credential")
    elif platform == "ghost":
        if username or not re.fullmatch(r"[a-fA-F0-9]{24}:[a-fA-F0-9]{64}", secret):
            raise CmsContractError("Invalid CMS credential")
    else:
        raise CmsContractError("Invalid CMS credential")
    return CmsCredential(platform=platform, username=username, secret=secret)


def seal_credential(binding, *, username="", secret):
    """Encrypt the exact provider/site/generation context with the existing keyring."""
    binding = validate_binding(binding)
    credential = _credential(binding["platform"], username, secret)
    return encrypt_credential_value(json.dumps({"schema_version": 1, "binding": binding,
        "username": credential.username, "secret": credential.secret}, separators=(",", ":"), sort_keys=True))


def validate_private_credential(credential, *, binding):
    """Revalidate a short-lived private value before preparing any HTTP headers."""
    if not isinstance(credential, CmsCredential) or credential.platform != binding["platform"]:
        raise CmsContractError("CMS credential context changed")
    return _credential(credential.platform, credential.username, credential.secret)


def open_credential(sealed, *, binding):
    """Decrypt only an exact original context; reject plaintext and unknown keys."""
    binding = validate_binding(binding)
    if not isinstance(sealed, str) or not sealed.startswith("mlai-enc:v1:") or len(sealed) > 8192:
        raise CmsContractError("CMS credential unavailable")
    try:
        value = json.loads(decrypt_credential_value(sealed))
        if (not isinstance(value, dict) or set(value) != {"schema_version", "binding", "username", "secret"} or
                type(value.get("schema_version")) is not int or value["schema_version"] != 1 or
                validate_binding(value["binding"]) != binding):
            raise CmsContractError("CMS credential context changed")
        return _credential(binding["platform"], value["username"], value["secret"])
    except (CredentialEncryptionError, ValueError, TypeError, UnicodeError):
        raise CmsContractError("CMS credential unavailable") from None
