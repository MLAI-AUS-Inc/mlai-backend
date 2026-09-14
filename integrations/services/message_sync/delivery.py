"""Fair public delivery claims and stale-worker fencing across adapter calls."""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from integrations.models import BridgeSyncState, CommunityBridgeChannel, CommunityBridgeDelivery
from .scheduler import LeaseLost

_current_delivery = ContextVar("message_sync_delivery", default=None)


@contextmanager
def delivery_context(delivery):
    token = _current_delivery.set((delivery["id"], delivery.get("lease_token"),
        delivery.get("_sync_state_id"), delivery.get("_previous_state_turn"), delivery.get("_claimed_state_turn")))
    try:
        yield
    finally:
        _current_delivery.reset(token)


def guard_delivery(row):
    """Call while holding the outbox row lock before any completion mutation."""
    claim = _current_delivery.get()
    if row.lease_token is None:
        if claim is not None and claim[1] is not None:
            raise LeaseLost("public_delivery_already_settled")
        return
    if (claim is None or claim[0] != row.pk or str(claim[1]) != str(row.lease_token)
            or row.lease_expires_at is None or row.lease_expires_at <= timezone.now()):
        raise LeaseLost("public_delivery_lease_lost")


def guard_current_delivery():
    claim = _current_delivery.get()
    if claim is not None:
        row = CommunityBridgeDelivery.objects.select_for_update().get(pk=claim[0])
        guard_delivery(row)


def refund_current_delivery_turn():
    """Lock state before the outbox row; quota denial did not serve a channel."""
    claim = _current_delivery.get()
    if claim is not None and claim[2] is not None:
        BridgeSyncState.objects.filter(pk=claim[2], last_served_at=claim[4]).update(last_served_at=claim[3])


def claim_public(limit):
    """One active delivery per channel; oldest-served channels get a turn first."""
    from integrations.services.community_bridge.store import _serialize_delivery
    from .history import ensure_state
    for channel in CommunityBridgeChannel.objects.filter(enabled=True, sync_state__isnull=True).order_by("id")[:100]:
        ensure_state(channel)
    result = []
    for _ in range(max(1, min(int(limit), 100))):
        now = timezone.now()
        ready = CommunityBridgeDelivery.objects.filter(
            channel_id=OuterRef("public_channel_id"),
            status__in=["pending", "failed", "waiting_parent"], available_at__lte=now,
            attempts__lt=F("max_attempts"),
        )
        active = CommunityBridgeDelivery.objects.filter(
            channel_id=OuterRef("public_channel_id"), status="processing",
        ).filter(Q(lease_expires_at__gt=now) | Q(lease_expires_at__isnull=True))
        with transaction.atomic():
            state = BridgeSyncState.objects.filter(public_channel__enabled=True).filter(
                Exists(ready), ~Exists(active),
            ).select_for_update(skip_locked=True, of=("self",)).order_by(
                F("last_served_at").asc(nulls_first=True), "id",
            ).first()
            if state is None:
                break
            row = CommunityBridgeDelivery.objects.filter(
                channel_id=state.public_channel_id, status__in=["pending", "failed", "waiting_parent"],
                available_at__lte=now, attempts__lt=F("max_attempts"),
            ).select_for_update(skip_locked=True).order_by("available_at", "id").first()
            if row is None:
                continue
            row.lease_token = uuid.uuid4()
            row.lease_expires_at = now + timedelta(seconds=120)
            row.status = "processing"
            row.locked_at = now
            row.attempts += 1
            row.save(update_fields=["lease_token", "lease_expires_at", "status", "locked_at", "attempts", "updated_at"])
            previous_turn = state.last_served_at
            state.last_served_at = now
            state.save(update_fields=["last_served_at"])
            result.append({**_serialize_delivery(row), "_sync_state_id": state.pk,
                "_previous_state_turn": previous_turn, "_claimed_state_turn": now})
    return result


def recover_public_leases():
    now = timezone.now()
    return CommunityBridgeDelivery.objects.filter(status="processing", lease_expires_at__lte=now).update(
        status="pending", locked_at=None, lease_token=None, lease_expires_at=None,
        available_at=now, updated_at=now,
        # A process crash is not a provider rejection. Keep the immutable request
        # and let the next worker safely retry it instead of exhausting attempts.
        attempts=F("attempts") - 1,
    )


@transaction.atomic
def supersede_stale_mutation(delivery_id):
    """A delayed older edit cannot overwrite an already delivered newer edit."""
    from django.db.models import DecimalField
    from django.db.models.functions import Cast
    from decimal import Decimal
    from integrations.services.community_bridge.store import complete_delivery
    row = CommunityBridgeDelivery.objects.select_for_update().get(pk=delivery_id)
    guard_delivery(row)
    if row.source_platform != "slack" or row.delivery_type not in {"edit", "delete", "reaction_add", "reaction_remove"} or not row.source_revision:
        return False
    peers = CommunityBridgeDelivery.objects.filter(
        channel_id=row.channel_id, source_platform=row.source_platform,
        source_channel_id=row.source_channel_id, source_message_id=row.source_message_id,
        target_platform=row.target_platform, status="completed", delivery_type__in=["edit", "delete", "reaction_add", "reaction_remove"],
        source_revision__regex=r"^[0-9]{1,20}\.[0-9]{1,6}$",
    ).exclude(pk=row.pk).annotate(revision=Cast("source_revision", DecimalField(max_digits=26, decimal_places=6)))
    if peers.filter(revision__gt=Decimal(row.source_revision)).exists():
        complete_delivery(delivery_id=row.pk)
        return True
    return False
