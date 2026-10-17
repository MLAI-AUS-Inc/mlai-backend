"""Canonical non-repository artifact envelope. It never grants provider access.

The backend reserves the exact original owner/company/policy/payment scope and
rechecks it before dispatch, every callback and any later publishing effect.
Content Factory may produce artifacts only; it must not infer CMS/hosting write
authority, credentials, publication readiness or approval from this envelope.
"""

from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

FIELDS = frozenset({"schema_version", "kind", "channel", "platform", "artifact_operation_id", "company_id",
    "organization_id", "owner_user_id", "connection_id", "connection_generation", "public_origin",
    "scope_sha256", "billing_request_id", "review_policy", "policy_sha256", "digest"})


class ArtifactContractError(ValueError):
    """A fixed diagnostic with no private caller values or worker/provider text."""


def canonical_bytes(value):
    """Stable cross-service bytes for exact scope and body-package identities."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    """Hash data for comparison, never as a substitute for backend authority."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def validate_envelope(value):
    """Require a typed closed envelope, excluding credentials and Git authority.

    A valid checksum is not authorization. The original private backend
    reservation and fresh current owner/site scope must also match exactly.
    """
    try:
        if (not isinstance(value, dict) or set(value) != FIELDS or type(value["schema_version"]) is not int or value["schema_version"] != 1 or
                value["kind"] != "publishing_channel_artifact" or value["channel"] not in {"cms", "hosted", "portable"} or
                value["review_policy"] not in {"auto", "review"} or
                not _uuid(value["artifact_operation_id"]) or not _uuid(value["company_id"]) or
                any(not isinstance(value[key], str) or re.fullmatch(r"[1-9][0-9]{0,18}", value[key]) is None for key in ("organization_id", "owner_user_id")) or
                any(not isinstance(value[key], str) or re.fullmatch(r"[a-f0-9]{64}", value[key]) is None for key in ("scope_sha256", "policy_sha256", "digest")) or
                not isinstance(value["billing_request_id"], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}", value["billing_request_id"]) is None):
            raise ArtifactContractError("Invalid channel artifact envelope")
        if value["channel"] == "cms":
            if value["platform"] not in {"wordpress", "ghost"}: raise ArtifactContractError("Invalid channel artifact platform")
        elif value["platform"] != "unknown": raise ArtifactContractError("Invalid channel artifact platform")
        if value["channel"] == "portable":
            if value["connection_id"] is not None or value["connection_generation"] is not None:
                raise ArtifactContractError("Portable artifacts cannot carry provider authority")
        elif (not _uuid(value["connection_id"]) or type(value["connection_generation"]) is not int or
                not 1 <= value["connection_generation"] < 10**12):
            raise ArtifactContractError("Invalid channel artifact connection")
        origin = value["public_origin"]
        if not isinstance(origin, str) or not 0 < len(origin) <= 1024 or any(ord(c) < 33 or 127 <= ord(c) < 160 for c in origin):
            raise ArtifactContractError("Invalid channel artifact origin")
        parsed = urlsplit(origin)
        host = parsed.hostname
        path = parsed.path
        if (parsed.scheme != "https" or not host or host != host.lower() or parsed.username is not None or parsed.password is not None or
                parsed.port is not None or parsed.query or parsed.fragment or not path.startswith("/") or not path.endswith("/") or
                "\\" in origin or "%" in path or "//" in path or any(part in {".", ".."} for part in path.split("/")) or
                "." not in host or re.fullmatch(r"[a-z0-9.-]{1,253}", host) is None or
                any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in host.split(".")) or
                urlunsplit(("https", host, path, "", "")) != origin):
            raise ArtifactContractError("Invalid channel artifact origin")
        if digest({key: item for key, item in value.items() if key != "digest"}) != value["digest"]:
            raise ArtifactContractError("Channel artifact envelope changed")
        return deepcopy(value)
    except ArtifactContractError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ArtifactContractError("Invalid channel artifact envelope") from None


def make_envelope(**fields):
    """Serialize a backend-reserved scope, before billing and worker dispatch."""
    try:
        value = {"schema_version": 1, "kind": "publishing_channel_artifact", **deepcopy(fields)}
        if "digest" in value:
            raise ArtifactContractError("A channel artifact digest cannot be supplied")
        value["digest"] = digest(value)
        return validate_envelope(value)
    except ArtifactContractError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ArtifactContractError("Invalid channel artifact envelope") from None
