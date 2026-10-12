"""Exact-payload human approvals and a durable, conservative delivery outbox."""

import re
import uuid
from datetime import timedelta

from django.db import transaction
from django.conf import settings
from django.utils import timezone

from .models import AgentActionProposal, AgentTask
from .policy import AgentPermissionDenied, authorize, bounded_json, canonical_hash, feature_enabled, validate_grant_base
from .runtime import AgentRuntimeError, _task_with_grant_lock, audit, locked, require_lease


ACTION_SCOPES = {"message.send": "messages:write"}


class ActionDefinitelyNotSent(Exception):
    """An executor may use this only when no external write could have occurred."""


def action_hash(*, task_id, generation, kind, resource, payload):
    return canonical_hash({"task_id": str(task_id), "generation": generation,
                           "kind": kind, "resource": resource, "payload": payload})


def validate_action_payload(kind, payload):
    if kind not in ACTION_SCOPES or not isinstance(payload, dict):
        raise AgentPermissionDenied("unsupported_action")
    if (set(payload) - {"text", "thread_id"} or not isinstance(payload.get("text"), str)
            or not payload["text"].strip() or not 1 <= len(payload["text"]) <= 12000):
        raise AgentPermissionDenied("invalid_action_payload")
    if "thread_id" in payload and (not isinstance(payload["thread_id"], str) or not re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,9}", payload["thread_id"])):
        raise AgentPermissionDenied("invalid_action_payload")
    if re.search(r"<!(?:channel|here|everyone)(?:\||>)", payload["text"], re.I):
        raise AgentPermissionDenied("broadcast_mentions_not_allowed")
    return bounded_json(payload, max_bytes=20000)


def attributed_payload(task, payload):
    """Attribution is part of the bytes the owner reviews and approves."""
    installation = task.grant.installation
    owner = installation.owner
    owner_name = owner.get_full_name() if callable(getattr(owner, "get_full_name", None)) else "member"
    def label(value):
        return re.sub(r"[<>&\x00-\x1f]", "", str(value or "member"))[:100]
    text = f"[Agent {label(installation.name)}, acting for {label(owner_name)}]\n\n{payload['text']}"
    return {**payload, "text": text}


def _get_action(action_id):
    task_id = AgentActionProposal.objects.values_list("task_id", flat=True).get(pk=action_id)
    task = _task_with_grant_lock(task_id)
    action = locked(AgentActionProposal.objects.all()).get(pk=action_id, task=task)
    action.task = task
    return action


def _check_action(action, *, access_checker=None, now=None, require_approved=False):
    now = now or timezone.now()
    task = action.task
    if not getattr(settings, "AGENT_RUNTIME_ACTIONS_ENABLED", False):
        raise AgentPermissionDenied("agent_actions_disabled")
    validate_grant_base(task.grant, expected_generation=action.generation, now=now)
    if task.status in {"cancelled", "failed"} or action.expires_at <= now:
        raise AgentPermissionDenied("action_inactive")
    if action.resource != task.resource:
        raise AgentPermissionDenied("task_resource_mismatch")
    if action.kind not in ACTION_SCOPES:
        raise AgentPermissionDenied("unsupported_action")
    authorize(task.grant, ACTION_SCOPES[action.kind], action.resource, access_checker=access_checker, now=now)
    payload = validate_action_payload(action.kind, action.payload)
    digest = action_hash(task_id=task.pk, generation=action.generation, kind=action.kind, resource=action.resource, payload=payload)
    if digest != action.payload_hash or (require_approved and action.approval_hash != digest):
        raise AgentPermissionDenied("action_changed")


@transaction.atomic
def propose_action(task_id, *, lease_token, lease_epoch, kind, resource, payload,
                   idempotency_key, access_checker=None, now=None):
    now = now or timezone.now()
    task = _task_with_grant_lock(task_id)
    require_lease(task, lease_token, lease_epoch, now=now)
    if resource != task.resource:
        raise AgentPermissionDenied("task_resource_mismatch")
    authorize(task.grant, "actions:propose", resource, access_checker=access_checker, now=now)
    if kind not in ACTION_SCOPES:
        raise AgentPermissionDenied("unsupported_action")
    payload = validate_action_payload(kind, attributed_payload(task, validate_action_payload(kind, payload)))
    from .policy import idempotency_key as clean_key
    key = clean_key(idempotency_key)
    digest = action_hash(task_id=task.pk, generation=task.generation, kind=kind, resource=resource, payload=payload)
    action, created = AgentActionProposal.objects.get_or_create(
        task=task, idempotency_key=key,
        defaults={"generation": task.generation, "kind": kind, "resource": resource,
                  "payload": payload, "payload_hash": digest,
                  "expires_at": min(task.grant.expires_at, now + timedelta(hours=24))},
    )
    if action.payload_hash != digest:
        raise AgentRuntimeError("idempotency_conflict")
    if created:
        audit("action_proposed", grant=task.grant, task=task, action_id=str(action.pk), payload_hash=digest)
    return action


@transaction.atomic
def approve_action(action_id, *, owner, expected_hash, access_checker=None, now=None):
    now = now or timezone.now()
    action = _get_action(action_id)
    if action.task.grant.installation.owner_id != owner.pk or not owner.is_active:
        raise AgentPermissionDenied("owner_mismatch")
    if action.status not in {"proposed", "approved"} or expected_hash != action.payload_hash:
        raise AgentRuntimeError("approval_conflict")
    _check_action(action, access_checker=access_checker, now=now)
    action.status, action.approved_by, action.approved_at = "approved", owner, now
    action.approval_hash = action.payload_hash
    action.save()
    audit("action_approved", grant=action.task.grant, task=action.task, action_id=str(action.pk), payload_hash=action.payload_hash)
    return action


@transaction.atomic
def reject_action(action_id, *, owner, now=None):
    action = _get_action(action_id)
    if action.task.grant.installation.owner_id != owner.pk or not owner.is_active:
        raise AgentPermissionDenied("owner_mismatch")
    if action.status not in {"proposed", "approved", "rejected"}:
        raise AgentRuntimeError("action_already_dispatched")
    action.status = "rejected"
    action.save(update_fields=("status", "updated_at"))
    audit("action_rejected", grant=action.task.grant, task=action.task, action_id=str(action.pk))
    return action


@transaction.atomic
def claim_action(action_id, *, access_checker=None, now=None):
    """Persist SENDING before making a network call; a crash cannot re-arm it."""
    now = now or timezone.now()
    action = _get_action(action_id)
    if action.status != "approved":
        raise AgentRuntimeError("action_not_approved")
    _check_action(action, access_checker=access_checker, now=now, require_approved=True)
    action.status, action.lease_token = "sending", uuid.uuid4()
    action.lease_expires_at = min(action.expires_at, now + timedelta(seconds=60))
    action.save()
    audit("action_dispatch_started", grant=action.task.grant, task=action.task, action_id=str(action.pk))
    return action


@transaction.atomic
def deliver_claimed_action(action_id, *, lease_token, executor, access_checker=None, now=None):
    """Trusted backend delivery only; never invoked by an agent credential.

    executor(action, idempotency_key=...) must use a bounded provider timeout
    (<60 seconds), a fixed service adapter, and server-resolved credentials. It
    must not follow an arbitrary model-supplied URL or use a caller-selected user.
    The canonical owner/grant locks stay held through the one call, fencing
    concurrent revocation. A process death leaves the prior SENDING commit.
    """
    now = now or timezone.now()
    action = _get_action(action_id)
    if action.status != "sending" or str(action.lease_token) != str(lease_token) or action.lease_expires_at <= now:
        raise AgentRuntimeError("action_lease_lost")
    try:
        _check_action(action, access_checker=access_checker, now=now, require_approved=True)
    except AgentPermissionDenied as exc:
        # No external call has started here. A transient authorization-source
        # outage can retry the exact approved operation after access recovers.
        action.status, action.error_code = ("approved" if exc.code == "resource_unavailable" else "cancelled"), exc.code
        action.lease_token, action.lease_expires_at = None, None
        action.save()
        return action
    try:
        receipt = executor(action, idempotency_key=f"agent-action:{action.pk}")
        if not isinstance(receipt, dict) or not receipt.get("external_id"):
            raise ValueError("Provider did not confirm an external identifier")
        action.receipt = bounded_json(receipt, max_bytes=4096)
        action.status, action.error_code = "sent", ""
    except ActionDefinitelyNotSent:
        action.status, action.error_code = "failed", "provider_rejected_before_send"
    except Exception:
        # Includes malformed acknowledgements and connection loss: the provider
        # may have accepted the write. Do not store provider exception secrets.
        action.status, action.error_code = "unknown", "delivery_unconfirmed"
    action.lease_token, action.lease_expires_at = None, None
    action.save()
    audit("action_delivery_result", grant=action.task.grant, task=action.task, action_id=str(action.pk), status=action.status)
    return action


def recover_actions(*, now=None, limit=100):
    now = now or timezone.now()
    if not feature_enabled():
        return 0
    ids = list(AgentActionProposal.objects.filter(status="sending", lease_expires_at__lte=now)
               .order_by("lease_expires_at").values_list("pk", flat=True)[:max(0, min(limit, 1000))])
    count = 0
    for action_id in ids:
        with transaction.atomic():
            action = _get_action(action_id)
            if action.status == "sending" and action.lease_expires_at <= now:
                action.status, action.error_code = "unknown", "sender_lease_expired"
                action.lease_token, action.lease_expires_at = None, None
                action.save()
                audit("action_delivery_unknown", grant=action.task.grant, task=action.task, action_id=str(action.pk))
                count += 1
    return count


@transaction.atomic
def reconcile_action(action_id, *, verifier, access_checker=None, now=None):
    """Trusted provider lookup; unknown writes cannot be approved/retried blindly.

    A confirmed receipt closes the action. Definitive non-delivery closes it as
    failed; an operator must create and approve a fresh proposal to try again.
    """
    action = _get_action(action_id)
    if action.status != "unknown":
        raise AgentRuntimeError("action_not_unknown")
    authorize(action.task.grant, "messages:read", action.resource,
              expected_generation=action.generation, access_checker=access_checker, now=now)
    outcome = verifier(action, idempotency_key=f"agent-action:{action.pk}")
    if isinstance(outcome, dict) and outcome.get("external_id"):
        action.status, action.receipt = "sent", bounded_json(outcome, max_bytes=4096)
    elif outcome is False:
        action.status = "failed"
    else:
        return action
    action.error_code = "reconciled"
    action.save()
    audit("action_reconciled", grant=action.task.grant, task=action.task, action_id=str(action.pk), status=action.status)
    return action
