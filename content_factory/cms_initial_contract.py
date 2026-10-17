"""Closed per-proposal metadata; initial consent and a key grant are distinct."""

from copy import deepcopy
from uuid import UUID

from . import cms_onboarding_plan as plans
from .cms_goal_contract import _time, _hex
from .channel_artifact_contract import canonical_bytes, digest

FIELDS = {"version", "kind", "operation_id", "stage_operation_id", "root_operation_id", "request_id", "access_epoch", "proposal_digest", "accepted_at", "expires_at", "digest"}


def uuid(value):
    """Require canonical resource/nonce identities, without granting authority."""
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("The initial CMS grant identity changed.")
    return value


def grant(*, operation_id, plan, request_id, access_epoch, proposal_digest, now):
    """Freeze one real owner key acceptance without renewing its goal or price."""
    plan = plans.validate(plan)
    uuid(operation_id); uuid(request_id)
    if (operation_id in {plan["operation_id"], plan["root_operation_id"], plan["credential_operation_id"], plan["binding"]["connection_id"]} or
            access_epoch is not None and not _hex(access_epoch) or not _hex(proposal_digest) or
            now < _time(plan["accepted_at"]) or access_epoch is None and
                (request_id != plan["native_intent"]["request_id"] or now != _time(plan["accepted_at"]))):
        raise ValueError("The initial CMS grant scope changed.")
    value = {"version": 1, "kind": "cms_initial_credential_grant", "operation_id": operation_id,
        "stage_operation_id": plan["operation_id"], "root_operation_id": plan["root_operation_id"], "request_id": request_id,
        "access_epoch": access_epoch, "proposal_digest": proposal_digest, "accepted_at": now.isoformat(), "expires_at": (now + plans.TTL).isoformat()}
    return {**value, "digest": digest(value)}


def validate(value, *, plan):
    """Reconstruct original grant time; a checksum is not current key consent."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("The initial CMS grant fields changed.")
    expected = grant(**{key: value[key] for key in ("operation_id", "request_id", "access_epoch", "proposal_digest")},
        plan=plan, now=_time(value["accepted_at"]))
    if canonical_bytes(value) != canonical_bytes(expected):
        raise ValueError("The initial CMS grant metadata changed.")
    return deepcopy(expected)
