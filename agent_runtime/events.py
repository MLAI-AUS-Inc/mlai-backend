"""Durable, resource-scoped event streams and bounded wake-up subscriptions."""

import re
import uuid
import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction, connection
from django.db.models import Q
from django.utils import timezone

from .models import AgentEvent, AgentSubscription
from .policy import (AgentPermissionDenied, authorize, bounded_json, canonical_hash,
                     feature_enabled, validate_resource)
from .runtime import AgentRuntimeError, _grant, audit, enqueue_task, locked


def lock_event_resource(resource):
    """Serialize resource writes/cursor initialization inside an atomic block.

    Acquiring before PostgreSQL allocates an event ID prevents a committed
    cursor from skipping a lower ID whose insertion commits later. Hash
    collisions only add serialization. SQLite serializes writes itself.
    """
    validate_resource(resource)
    if not connection.in_atomic_block:
        raise AgentRuntimeError("event_lock_requires_transaction")
    if connection.vendor == "postgresql":
        bucket = int.from_bytes(hashlib.sha256(resource.encode()).digest()[:4], "big", signed=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [1095911252, bucket])
    elif connection.vendor != "sqlite":
        raise AgentRuntimeError("unsupported_event_database")


@transaction.atomic
def ingest_event(*, source, source_event_id, resource, event_type, payload,
                 actor_kind="human", chain_id=None, chain_depth=0, occurred_at=None):
    """Called only after a trusted producer verifies signature/replay/source ACL.

    This is not a public member ingress. Do not let a request body assert that
    its author is human, or relabel private mirrored content as an allowed source.
    """
    if not feature_enabled():
        raise AgentPermissionDenied("agent_runtime_disabled")
    validate_resource(resource)
    if (not isinstance(source, str) or not re.fullmatch(r"[a-z0-9_.-]{1,40}", source)
            or not isinstance(source_event_id, str) or not 1 <= len(source_event_id) <= 200
            or not isinstance(event_type, str) or not re.fullmatch(r"[a-z0-9_.-]{1,80}", event_type)
            or actor_kind not in {"human", "agent", "system"}
            or isinstance(chain_depth, bool) or not isinstance(chain_depth, int) or not 0 <= chain_depth <= 8):
        raise AgentRuntimeError("invalid_event")
    try:
        chain = uuid.UUID(str(chain_id)) if chain_id else uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{source_event_id}")
    except (ValueError, TypeError) as exc:
        raise AgentRuntimeError("invalid_event_chain") from exc
    material = {"resource": resource, "event_type": event_type, "payload": bounded_json(payload, max_bytes=16384),
                "actor_kind": actor_kind, "chain_id": str(chain), "chain_depth": chain_depth}
    digest = canonical_hash(material)
    lock_event_resource(resource)
    event, created = AgentEvent.objects.get_or_create(
        source=source, source_event_id=source_event_id,
        defaults={**material, "chain_id": chain, "payload_hash": digest,
                  "occurred_at": occurred_at or timezone.now()},
    )
    if event.payload_hash != digest:
        raise AgentRuntimeError("event_idempotency_conflict")
    return event


def _cursor_key():
    return hashlib.sha256(("agent-events-v1:" + settings.SECRET_KEY).encode()).digest()


def encode_cursor(grant, resource, position):
    content = {"v": 1, "grant": str(grant.pk), "generation": grant.generation,
               "resource": resource, "position": position}
    return Fernet(base64.urlsafe_b64encode(_cursor_key())).encrypt(json.dumps(content, sort_keys=True).encode()).decode()


def decode_cursor(grant, resource, cursor):
    if cursor == "":
        return 0
    if not isinstance(cursor, str) or len(cursor) > 2048:
        raise AgentRuntimeError("invalid_event_cursor")
    try:
        content = json.loads(Fernet(base64.urlsafe_b64encode(_cursor_key())).decrypt(cursor.encode(), ttl=7 * 86400))
    except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
        raise AgentRuntimeError("invalid_event_cursor") from exc
    position = content.get("position") if isinstance(content, dict) else None
    if (not isinstance(content, dict) or content.get("v") != 1 or content.get("grant") != str(grant.pk)
            or content.get("generation") != grant.generation or content.get("resource") != resource
            or isinstance(position, bool) or not isinstance(position, int) or position < 0):
        raise AgentRuntimeError("invalid_event_cursor")
    return position


def event_payload(event, grant):
    event_id = hmac.new(_cursor_key(), f"{grant.pk}:{grant.generation}:{event.pk}".encode(), hashlib.sha256).hexdigest()[:32]
    return {"id": "event_" + event_id, "source": event.source,
            "event_type": event.event_type, "resource": event.resource,
            "payload": event.payload, "actor_kind": event.actor_kind,
            "chain_id": str(event.chain_id), "chain_depth": event.chain_depth,
            "occurred_at": event.occurred_at.isoformat()}


@transaction.atomic
def read_events(grant, *, resource, cursor="", limit=50, access_checker=None, now=None):
    grant = _grant(grant.pk)
    authorize(grant, "events:read", resource, access_checker=access_checker, now=now)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise AgentRuntimeError("invalid_event_cursor")
    position = decode_cursor(grant, resource, cursor)
    rows = list(AgentEvent.objects.filter(resource=resource, pk__gt=position).order_by("pk")[:limit])
    return {"events": [event_payload(event, grant) for event in rows],
            "cursor": encode_cursor(grant, resource, rows[-1].pk if rows else position)}


def should_wake(subscription, event):
    if not subscription.is_active or event.resource != subscription.resource:
        return False
    if event.event_type not in subscription.event_types:
        return False
    if event.actor_kind == "agent" and not subscription.allow_agent_events:
        return False
    return event.chain_depth < min(subscription.max_chain_depth, 3)


def dispatch_subscriptions(*, access_checker=None, limit=100, events_per_subscription=50, now=None):
    if not feature_enabled():
        return 0
    now = now or timezone.now()
    emitted = 0
    ids = list(AgentSubscription.objects.filter(is_active=True)
               .filter(Q(delivery__mode__isnull=True) | ~Q(delivery__mode="webhook")).order_by("updated_at")
               .values_list("pk", flat=True)[:max(0, min(limit, 1000))])
    for subscription_id in ids:
        with transaction.atomic():
            grant_id = AgentSubscription.objects.values_list("grant_id", flat=True).get(pk=subscription_id)
            grant = _grant(grant_id)
            subscription = locked(AgentSubscription.objects.all()).get(pk=subscription_id)
            if not subscription.is_active or subscription.delivery.get("mode") == "webhook":
                continue
            try:
                authorize(grant, "events:read", subscription.resource,
                          expected_generation=subscription.generation, access_checker=access_checker, now=now)
            except AgentPermissionDenied as exc:
                if exc.code == "resource_unavailable":
                    subscription.save(update_fields=("updated_at",))
                    continue
                subscription.is_active = False
                subscription.save(update_fields=("is_active", "updated_at"))
                audit("subscription_stopped", grant=grant, reason=exc.code, subscription_id=str(subscription.pk))
                continue
            events = AgentEvent.objects.filter(resource=subscription.resource, pk__gt=subscription.cursor).order_by("pk")[:max(1, min(events_per_subscription, 100))]
            for event in events:
                if should_wake(subscription, event) and subscription.instruction:
                    try:
                        enqueue_task(grant, instruction=subscription.instruction,
                                     resource=subscription.resource,
                                     idempotency_key=f"event:{subscription.pk}:{event.pk}",
                                     inputs=[{"event": event_payload(event, grant), "untrusted_source": True}],
                                     limits=subscription.limits, delivery=subscription.delivery,
                                     access_checker=access_checker, now=now)
                    except AgentRuntimeError as exc:
                        if exc.code not in {"daily_task_limit", "daily_cost_limit"}:
                            raise
                        # Keep this event pending when admission is temporarily
                        # budget-limited; task creation and cursor advance commit together.
                        break
                    except AgentPermissionDenied as exc:
                        # Source access can change between subscription and
                        # task admission. One denied subscription must not stop
                        # the scheduler or consume that event's cursor.
                        if exc.code != "resource_unavailable":
                            subscription.is_active = False
                            audit("subscription_stopped", grant=grant, reason=exc.code,
                                  subscription_id=str(subscription.pk))
                        break
                    emitted += 1
                subscription.cursor = event.pk
            subscription.save(update_fields=("cursor", "is_active", "updated_at"))
    return emitted
