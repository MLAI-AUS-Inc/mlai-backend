"""Closed original credential request metadata; no checksum grants access."""

from copy import deepcopy
from datetime import timedelta
import re
from uuid import UUID

from . import cms_goal_contract as goals
from .cms_connection_contract import validate_binding
from .channel_artifact_contract import canonical_bytes, digest

FIELDS = {"schema_version", "kind", "operation_id", "root_operation_id", "root_action", "binding", "configuration_revision",
    "credential_operation_id", "credential_digest", "proposal_digest", "principal_id", "request_id", "channel_choice_digest", "request_digest", "access_epoch", "accepted_at", "expires_at", "digest"}
TTL = timedelta(days=1)


class CmsAccessContractError(ValueError):
    """A private original owner grant changed or cannot be verified."""


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise CmsAccessContractError("The original CMS access reference changed.")
    return value


def make_intent(*, operation_id, root_operation_id, root_action, binding, configuration_revision, credential_operation_id,
                credential_digest, proposal_digest, principal_id, request_id, channel_choice_digest, request_digest, access_epoch, now):
    """Freeze only references selected by the actual owner acceptance boundary.

    Encrypted proposed credentials are stored separately on the private job.
    Every use must independently reload the real root, owner and current key.
    """
    try:
        resources = [_uuid(identifier) for identifier in (operation_id, root_operation_id, credential_operation_id)]
        _uuid(request_id)
        binding = validate_binding(binding)
        if (len(set(resources)) != 3 or root_action not in {"cms-setup", "cms-goal"} or type(configuration_revision) is not int or
                not 0 <= configuration_revision < 10**12 or any(not goals._hex(value) for value in (credential_digest, proposal_digest, channel_choice_digest, request_digest, access_epoch)) or
                not isinstance(principal_id, str) or re.fullmatch(r"[1-9][0-9]{0,18}" if binding["platform"] == "wordpress" else r"[a-f0-9]{24}", principal_id) is None):
            raise CmsAccessContractError("The original CMS access scope changed.")
        at = goals._time(now.isoformat())
        value = {"schema_version": 1, "kind": "cms_credential_access_intent", "operation_id": operation_id,
            "root_operation_id": root_operation_id, "root_action": root_action, "binding": deepcopy(binding),
            "configuration_revision": configuration_revision, "credential_operation_id": credential_operation_id,
            "credential_digest": credential_digest, "proposal_digest": proposal_digest, "principal_id": principal_id, "request_id": request_id,
            "channel_choice_digest": channel_choice_digest, "request_digest": request_digest, "access_epoch": access_epoch,
            "accepted_at": at.isoformat(), "expires_at": (at + TTL).isoformat()}
        return {**value, "digest": digest(value)}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsAccessContractError("The original CMS access request changed.") from None


def wait_epoch(operation):
    """Identify an actual recorded access failure, distinct from a later wait."""
    from .website_failure_contract import validate_failure
    try:
        receipt = operation.receipt
        failure = validate_failure(receipt.get("failure"))
        occurrence = receipt.get("failure_occurrence_id")
        if (operation.state != "needs_user" or (receipt.get("user_action") or {}).get("id") != "connect_cms" or
                failure["failure_class"] != "needs_user" or not isinstance(occurrence, str) or not 0 < len(occurrence) <= 200 or
                any(ord(char) < 32 for char in occurrence)):
            return None
        return digest({"root_operation_id": str(operation.pk), "failure_occurrence_id": occurrence})
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def validate_intent(value):
    """Validate closed immutable metadata, independently of current permission."""
    try:
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise CmsAccessContractError("The original CMS access fields changed.")
        expected = make_intent(**{key: item for key, item in value.items() if key not in {
            "schema_version", "kind", "accepted_at", "expires_at", "digest"}}, now=goals._time(value["accepted_at"]))
        if canonical_bytes(value) != canonical_bytes(expected):
            raise CmsAccessContractError("The original CMS access metadata changed.")
        return deepcopy(expected)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsAccessContractError("The original CMS access request changed.") from None
